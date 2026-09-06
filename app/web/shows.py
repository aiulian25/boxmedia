"""One series' record, and the decision it exists for (TV step 12).

Built on `movies.py`'s exact shape: a real page at `/shows/{tmdb_id}` that also answers
`?fragment=1` with the inner block, so the modal is an enhancement rather than the only
way in. Everything the page needs arrives in ONE TMDB request — `append_to_response`
brings the seasons, the cast, the rating and the TVDB bridge with the show itself.

## The add block asks exactly one question

Quality, root folder, series type and season folders are **per connection**, set once in
Settings. Asking them per title would break the rule the film add already keeps and turn
one decision into five. `monitor` is the exception because it is the only one of them
that is a fact about THIS show rather than about the server: all of it, only what has
yet to air, a taster, or nothing yet.

## Nothing is a duplicate

Sonarr keys series on TVDB ids. A Trakt row carries one for free; a TMDB row is bridged
through `external_ids` first, and a show TMDB has no TVDB id for is refused honestly with
a Sonarr search offered instead — never an Add that could not work. Before posting, the
library snapshot is consulted: a series already there gets "Open in Sonarr" rather than a
second copy. That check is a lookup, never a string match on Sonarr's error body — the
teardown's own finding (`tv-discovery.md` §11).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.audit import AuditAction
from app.services.apps import (
    KIND_SONARR,
    InvalidAppError,
)
from app.services.discovery import PROVIDER_TMDB, DiscoveryError
from app.services.ignore import KIND_SERIES
from app.services.matcher import normalize_title
from app.services.posters import HEADSHOT_WIDTH, SERIES_POSTER_WIDTH
from app.services.reports import imdb_url
from app.services.sonarr import (
    MONITOR_ALL,
    MONITOR_OPTIONS,
    SERIES_TYPE_STANDARD,
    SonarrError,
)
from app.services.tmdb import TmdbClient, TmdbError
from app.web.deps import (
    cache_posters,
    client_ip,
    current_user,
    optional_int,
    refresh_series_library,
    render,
    sonarr_client_for,
)
from app.web.profile import STATUS_QUERY_KEY

router = APIRouter()

NAV_KEY = "discover"  # a series is reached from Discover; keep that nav item lit
SHOW_PATH = "/shows/{tmdb_id}"
ADD_SERIES_PATH = "/add-series"
IGNORE_SERIES_PATH = "/ignore-series"
FRAGMENT_PARAM = "fragment"

# The same hard bound the movie modal uses. A detail page that hangs on TMDB is a page
# nobody can close.
DETAIL_TIMEOUT_SECONDS = 6.0
ADD_TIMEOUT_SECONDS = 10.0
MAX_CAST = 12

UNAVAILABLE_MESSAGE = (
    "Add a TMDB API key under Discovery in Settings to see series details."
)
# Said instead of the line above when a key IS stored and will not decrypt. "Add a key"
# is the wrong advice for one that is already there, and the thing to fix is the
# encryption key rather than the credential.
UNREADABLE_KEY_MESSAGE = (
    "Your saved TMDB key cannot be read with this install’s encryption key. "
    "Restore the key file, or re-enter the key under Discovery in Settings."
)

# What Monitor offers, in the words a person would use. Sonarr's own values are the
# keys — passed through verbatim, because "firstSeason" is camelCase because their enum
# is, and a tidied spelling would be silently rejected.
MONITOR_CHOICES = (
    (MONITOR_ALL, "All episodes"),
    ("future", "Future episodes only"),
    ("firstSeason", "First season only"),
    ("none", "Nothing yet — I'll choose later"),
)


class ShowStatus:
    """Why the page reloaded. A closed enum — never text from anywhere else."""

    ADDED = "series_added"
    ALREADY = "series_already"
    NO_TVDB = "series_no_tvdb"
    ADD_CONFIG = "series_add_config"
    ADD_FAILED = "series_add_failed"
    IGNORED = "series_ignored"
    UNIGNORED = "series_unignored"


STATUS_MESSAGES = {
    ShowStatus.ADDED: ("success", "Added to Sonarr at that connection's quality."),
    ShowStatus.ALREADY: (
        "error",
        "That series is already in Sonarr — not adding a duplicate.",
    ),
    ShowStatus.NO_TVDB: (
        "error",
        "TMDB has no TVDB id for this series, and Sonarr identifies series by TVDB id. "
        "Search for it in Sonarr by name instead.",
    ),
    ShowStatus.ADD_CONFIG: (
        "error",
        "Set a Sonarr connection with a quality profile and a root folder in Settings first.",
    ),
    ShowStatus.ADD_FAILED: (
        "error",
        "Sonarr rejected the request — check the connection and try again.",
    ),
    ShowStatus.IGNORED: ("success", "Ignored — it won't be suggested again."),
    ShowStatus.UNIGNORED: ("success", "Removed from your ignore list."),
}


def _tmdb_client(request: Request) -> TmdbClient | None:
    """A TMDB client from the stored key, or None when there is none to build one from.

    None also covers a stored key this install's encryption key cannot open;
    `_missing_client_message` asks which of the two it was, because the advice differs.
    """
    try:
        key = request.app.state.discovery.decrypt(PROVIDER_TMDB)
    except DiscoveryError:
        return None
    if not key:
        return None
    # Deliberately WITHOUT the app's outbound-TLS settings, unlike every Radarr, Sonarr
    # and media-server client. That escape hatch exists for the user's OWN servers, and
    # `BM_TLS_CA_FILE` becomes a context trusting only that CA — which TMDB's public
    # certificate then fails. Passing it here broke the show page for every install with
    # a self-signed home server, while Discover's own refresh (which never passed it)
    # kept working. The README's promise is the rule: public endpoints are always
    # verified, against the system trust store.
    return TmdbClient(key, timeout=DETAIL_TIMEOUT_SECONDS)


def _missing_client_message(request: Request) -> str:
    """Why there is no TMDB client: nothing stored, or stored and unreadable."""
    if request.app.state.discovery.load().has_tmdb:
        return UNREADABLE_KEY_MESSAGE
    return UNAVAILABLE_MESSAGE


def _sonarr_targets(request: Request) -> list[dict[str, object]]:
    """Every Sonarr connection, by the name the user gave it, primary first.

    `ready` decides whether an entry can be clicked: a connection with no resolvable
    quality profile or root folder would only fail at the far end, and saying so here
    beats a failed add that looks like a network problem.
    """
    primary_id = request.app.state.apps.primary_id(KIND_SONARR)
    cache = request.app.state.sonarr_options
    targets = []
    for app in request.app.state.apps.list_apps(KIND_SONARR):
        options = cache.load(app.id)
        profile_id = app.quality_profile_id
        folder = app.root_folder
        if folder is None and options.root_folders:
            folder = options.root_folders[0]
        if profile_id is None and options.profiles:
            profile_id = options.profiles[0].id
        targets.append({
            "id": app.id,
            "name": app.name,
            "primary": app.id == primary_id,
            "profile_name": options.profile_name(profile_id),
            "folder": folder,
            "ready": profile_id is not None and bool(folder),
        })
    targets.sort(key=lambda entry: not entry["primary"])
    return targets


async def _headshots(request: Request, people: list) -> list[dict]:
    """Cast rows with their portraits cached locally, so the CSP stays img-src 'self'."""
    views = [
        {"name": person.name, "role": person.role, "poster_url": person.headshot_url}
        for person in people
    ]
    await cache_posters(request, views, width=HEADSHOT_WIDTH)
    for view in views:
        view["headshot"] = view.pop("poster_local", None)
    return views


@router.get(SHOW_PATH)
async def show_detail(request: Request, tmdb_id: int) -> HTMLResponse:
    """One series' record. `?fragment=1` returns the inner block for the modal."""
    current_user(request)
    fragment = request.query_params.get(FRAGMENT_PARAM) == "1"
    template = "_show_detail.html" if fragment else "show_detail.html"

    client = _tmdb_client(request)
    if client is None:
        return render(
            request, template, show=None, error=_missing_client_message(request)
        )
    try:
        detail = await asyncio.wait_for(
            client.tv_detail(tmdb_id), timeout=DETAIL_TIMEOUT_SECONDS
        )
    except (TmdbError, TimeoutError):
        return render(
            request, template, show=None,
            error="TMDB could not be reached — try again shortly.",
        )

    held = request.app.state.series_cache.snapshot().find(
        tvdb_id=detail.tvdb_id, title=detail.title, year=detail.year
    )
    server_state, server_name = _server_state(request, detail)
    ignored = request.app.state.ignore.is_ignored(
        detail.tmdb_id, normalize_title(detail.title), KIND_SERIES
    )

    poster_holder = [{"poster_url": detail.poster_url}]
    await cache_posters(request, poster_holder, width=SERIES_POSTER_WIDTH)
    cast = await _headshots(request, list(detail.cast[:MAX_CAST]))
    banner = STATUS_MESSAGES.get(request.query_params.get(STATUS_QUERY_KEY, ""))
    targets = _sonarr_targets(request)

    return render(
        request,
        template,
        active_nav=NAV_KEY,
        banner_kind=banner[0] if banner else None,
        banner_text=banner[1] if banner else None,
        targets=targets,
        show_targets=len(targets) > 1,
        monitor_choices=MONITOR_CHOICES,
        default_monitor=MONITOR_ALL,
        add_series_path=ADD_SERIES_PATH,
        ignore_series_path=IGNORE_SERIES_PATH,
        held=held,
        server_state=server_state,
        server_name=server_name,
        ignored=ignored,
        show={
            "tmdb_id": detail.tmdb_id,
            "tvdb_id": detail.tvdb_id,
            "title": detail.title,
            "year": detail.year,
            "overview": detail.overview,
            "genres": detail.genres,
            "networks": detail.networks,
            "status": detail.status,
            "certification": detail.certification,
            "rating": detail.rating,
            "runtime": detail.episode_run_time,
            "seasons": detail.seasons,
            "first_air_date": _day_first(detail.first_air_date),
            "poster_local": poster_holder[0].get("poster_local"),
            "imdb_url": imdb_url(detail.imdb_id),
            "trailer_url": detail.trailer_url,
            "addable": detail.addable,
            "cast": cast,
        },
    )


def _day_first(value: object) -> str | None:
    """A date as day/month/year. Never American — the standing rule for this app."""
    return f"{value.day}/{value.month}/{value.year}" if value else None


def _server_state(request: Request, detail: object) -> tuple[str | None, str | None]:
    """Whether the media server already holds this series, and what it is called."""
    stored = request.app.state.media_server.load()
    if stored is None:
        return None, None
    cached = request.app.state.media_server_cache.load()
    if cached is None:
        return None, stored.name
    return (
        cached[0].holds_series(
            detail.tvdb_id, detail.tmdb_id, detail.imdb_id, detail.title, detail.year
        ),
        stored.name,
    )


def _redirect(request: Request, tmdb_id: int | None, status_code: str) -> RedirectResponse:
    """Back to the series' own page, with a closed status code.

    The destination is derived here and never taken from the request, so no crafted form
    can bounce anyone off this app.
    """
    base = request.app.state.settings.url_base
    target = f"{base}/shows/{tmdb_id}" if tmdb_id else f"{base}/discover"
    return RedirectResponse(
        f"{target}?{STATUS_QUERY_KEY}={status_code}",
        status_code=http_status.HTTP_303_SEE_OTHER,
    )


def _resolve_target(request: Request, requested: str) -> str | None:
    """Which Sonarr an add goes to, or None when the request names one we do not have.

    The browser chooses WHICH configured connection and nothing else: an id that is not
    in apps.yml is refused rather than trusted, so no crafted form can aim an add at an
    arbitrary host. Empty means the Sonarr primary, which is what the plain button posts.
    """
    requested = requested.strip()
    if not requested:
        return request.app.state.apps.primary_id(KIND_SONARR)
    known = {app.id for app in request.app.state.apps.list_apps(KIND_SONARR)}
    return requested if requested in known else None


@router.post(ADD_SERIES_PATH)
async def add_series(
    request: Request,
    tmdb_id: str = Form(...),
    title: str = Form(...),
    tvdb_id: str = Form(""),
    monitor: str = Form(MONITOR_ALL),
    target: str = Form(""),
) -> RedirectResponse:
    """Add one series to a chosen Sonarr — never a duplicate, never a guess."""
    user = current_user(request)
    tmdb = optional_int(tmdb_id)
    app_id = _resolve_target(request, target)
    if app_id is None:
        return _redirect(request, tmdb, ShowStatus.ADD_CONFIG)
    if monitor not in MONITOR_OPTIONS:
        # Closed enum. The form offers four; anything else is a bug or an attack.
        return _redirect(request, tmdb, ShowStatus.ADD_CONFIG)

    app = request.app.state.apps.get(app_id)
    options = request.app.state.sonarr_options.load(app_id)
    profile_id = app.quality_profile_id
    folder = app.root_folder
    if profile_id is None and options.profiles:
        profile_id = options.profiles[0].id
    if folder is None and options.root_folders:
        folder = options.root_folders[0]
    if profile_id is None or not folder:
        return _redirect(request, tmdb, ShowStatus.ADD_CONFIG)

    tvdb = optional_int(tvdb_id) or await _bridge(request, tmdb)
    if tvdb is None:
        # Honest refusal. Sonarr keys series on TVDB ids and TMDB has none for this show,
        # so no button here could have worked; the page offers a Sonarr search instead.
        return _redirect(request, tmdb, ShowStatus.NO_TVDB)

    # The duplicate guard is a snapshot lookup, never a string match on Sonarr's error
    # body — tv-discovery.md §11 found exactly that resting on the literal phrases
    # "SeriesExistsValidator" and "already been added", so any rewording broke it.
    if request.app.state.series_cache.snapshot().find(tvdb_id=tvdb) is not None:
        return _redirect(request, tmdb, ShowStatus.ALREADY)

    try:
        client = sonarr_client_for(request, app_id, timeout=ADD_TIMEOUT_SECONDS)
        await asyncio.wait_for(
            client.add_series(
                tvdb_id=tvdb,
                title=title,
                quality_profile_id=profile_id,
                root_folder_path=folder,
                monitor=monitor,
                season_folder=app.season_folders is not False,
                series_type=app.series_type or SERIES_TYPE_STANDARD,
                search_on_add=app.search_on_add is not False,
            ),
            timeout=ADD_TIMEOUT_SECONDS,
        )
    except (SonarrError, TimeoutError, InvalidAppError, KeyError):
        return _redirect(request, tmdb, ShowStatus.ADD_FAILED)

    request.app.state.audit.record(
        AuditAction.SERIES_ADDED,
        actor=user.username, source_ip=client_ip(request),
        title=title, tvdb_id=tvdb, app_id=app_id, monitor=monitor,
    )
    # Re-read the library so the card is right immediately rather than after the next
    # scheduled refresh — the same "try again now" reasoning the Test button uses.
    await refresh_series_library(request, app_id)
    return _redirect(request, tmdb, ShowStatus.ADDED)


async def _bridge(request: Request, tmdb_id: int | None) -> int | None:
    """The TVDB id for a TMDB-sourced row, or None when TMDB has none.

    A Trakt row already carries one and posts it, so this costs nothing there. Bounded,
    best-effort, and None on any failure — which the caller turns into an honest refusal
    rather than an add that would create the wrong series.
    """
    if tmdb_id is None:
        return None
    client = _tmdb_client(request)
    if client is None:
        return None
    try:
        return await asyncio.wait_for(
            client.tvdb_id_for(tmdb_id), timeout=DETAIL_TIMEOUT_SECONDS
        )
    except (TmdbError, TimeoutError):
        return None


@router.post(IGNORE_SERIES_PATH)
def ignore_series(
    request: Request,
    tmdb_id: str = Form(...),
    title: str = Form(...),
    undo: str = Form(""),
) -> RedirectResponse:
    """Stop suggesting this series — or start again.

    Stored with `kind=series`, so it can never collide with a film: TMDB numbers films
    and series in separate namespaces, and without the marker ignoring tv/1396 would hide
    movie/1396 from the weekly chart.
    """
    current_user(request)
    tmdb = optional_int(tmdb_id)
    store = request.app.state.ignore
    normalized = normalize_title(title)
    if undo:
        store.remove(tmdb_id=tmdb, normalized_title=normalized, kind=KIND_SERIES)
        return _redirect(request, tmdb, ShowStatus.UNIGNORED)
    store.add(
        tmdb_id=tmdb, title=title, normalized_title=normalized, kind=KIND_SERIES
    )
    return _redirect(request, tmdb, ShowStatus.IGNORED)
