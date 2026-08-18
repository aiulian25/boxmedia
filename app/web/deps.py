"""Shared web helpers: templating and per-request accessors.

Handlers behind the gate can assume an authenticated user (the middleware in
`app.main` redirects otherwise); `current_user` simply surfaces what the
middleware already attached to the request.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core import security
from app.core.sessions import COOKIE_NAME
from app.services.apps import ExternalApp, client_for_credentials
from app.services.mediaserver import (
    LIBRARY_CACHE_TTL_SECONDS,
    RENDER_FETCH_TIMEOUT_SECONDS,
    MediaServerError,
    MediaServerSnapshot,
    snapshot_from_movies,
)
from app.services.posters import POSTER_WIDTH, sized
from app.services.radarr import RadarrClient, RadarrError, RadarrMovie
from app.services.radarr_options import RadarrOptions, fetch_options
from app.services.users import THEME_DARK, User

BRAND_NAME = "BoxMedia"
RADARR_OPTIONS_TIMEOUT_SECONDS = 4.0
RADARR_LIBRARY_TIMEOUT_SECONDS = 4.0
# How long a connection that just failed is left alone before a page bothers it again.
# Shorter than the poster cache's 300s equivalent: a Radarr comes back on a timescale a
# person notices, and the cost of guessing wrong is only that one page renders without a
# library it could have had. Long enough that a dead box costs one timeout a minute
# rather than one per page view.
RADARR_RETRY_AFTER_SECONDS = 60.0
LOGIN_PATH = "/login"  # exempt from the CSRF check — no session exists yet
CSRF_REJECTED_DETAIL = "cross-origin request rejected"
# What each action reports back to the page it returns to. Shared, because more
# than one router now lands a user on a page that has to explain an add: the weekly
# view and the film's own page, which is where an add with no report context returns.
BANNER_SUCCESS = "success"
BANNER_ERROR = "error"
DETAIL_STATUS_MESSAGES = {
    "added": (BANNER_SUCCESS, "Added to Radarr at that connection’s quality."),
    "already_in_radarr": (
        BANNER_ERROR,
        "That title is already in Radarr — not adding a duplicate.",
    ),
    "add_config": (
        BANNER_ERROR,
        "Set a Radarr connection, a default root folder, and a quality profile in Settings first.",
    ),
    "add_failed": (
        BANNER_ERROR,
        "Radarr rejected the request — check the connection and try again.",
    ),
    "upgraded": (BANNER_SUCCESS, "Quality updated — Radarr is searching for the new version."),
    "ignored": (BANNER_SUCCESS, "Ignored — it won’t be added on any week."),
    "unignored": (BANNER_SUCCESS, "Removed from your ignore list."),
    "match_fixed": (BANNER_SUCCESS, "Match updated — you can add the title now."),
    "unchanged": (
        BANNER_SUCCESS,
        "Already up to date — this week’s chart hasn’t changed, so nothing was added.",
    ),
}
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


class RadarrBackoff:
    """Which connections recently failed a best-effort read, so pages stop waiting on them.

    A down Radarr costs the full 4s timeout per connection per render — on the dashboard,
    the weekly view, the search modal and the movie modal alike. The poster cache already
    solves this shape of problem for image hosts (posters.FAILED_RETRY_AFTER_SECONDS);
    this is the same idea for the Radarr reads that merely decorate a page.

    Deliberately NOT consulted by anything whose job is to find out whether a box is back:
    the Settings health dots, Test Connection, and the scheduler's own run all still
    really try, every time. A backoff that suppressed those would hide recovery instead of
    surviving an outage.

    Bounded by the number of configured connections, and per app instance rather than
    global, so tests and multiple apps stay isolated.
    """

    def __init__(self, retry_after_seconds: float = RADARR_RETRY_AFTER_SECONDS) -> None:
        self._retry_after = retry_after_seconds
        self._failed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_skip(self, app_id: str) -> bool:
        """True while a recent failure should still be honoured, expiring the entry once
        it is old enough to be worth another attempt."""
        with self._lock:
            failed_at = self._failed_at.get(app_id)
            if failed_at is None:
                return False
            if time.monotonic() - failed_at < self._retry_after:
                return True
            del self._failed_at[app_id]
            return False

    def note_failure(self, app_id: str) -> None:
        with self._lock:
            self._failed_at[app_id] = time.monotonic()

    def note_success(self, app_id: str) -> None:
        """Answered — or its details were just edited, which is a reason to try again now
        rather than after the wait."""
        with self._lock:
            self._failed_at.pop(app_id, None)

    def forget(self, app_id: str) -> None:
        """Drop a removed connection's entry so the map cannot outlive apps.yml."""
        self.note_success(app_id)


def radarr_backoff(request: Request) -> RadarrBackoff:
    """This app instance's backoff map."""
    return request.app.state.radarr_backoff


async def csrf_guard(request: Request) -> None:
    """Reject mutating requests that don't carry this session's CSRF token.

    Runs as a router-level dependency rather than in the hardening middleware on purpose:
    `request.form()` is cached on this same Request object, so the endpoint's own Form
    parameters re-use the parsed body instead of reading a consumed stream. Login is
    exempt (no session exists yet) and keeps the Origin check plus the rate limiter.
    """
    if request.method not in security.MUTATING_METHODS:
        return
    settings = request.app.state.settings
    if request.url.path == f"{settings.url_base}{LOGIN_PATH}":
        return
    form = await request.form()
    supplied = form.get("csrf_token")
    session_id = request.cookies.get(COOKIE_NAME)
    if not security.csrf_valid(settings.session_secret, session_id, supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_REJECTED_DETAIL
        )


def render(
    request: Request,
    template_name: str,
    *,
    status_code: int = status.HTTP_200_OK,
    active_nav: str | None = None,
    **context: object,
) -> HTMLResponse:
    settings = request.app.state.settings
    session_id = request.cookies.get(COOKIE_NAME, "")
    user = getattr(request.state, "user", None)
    base_context: dict[str, object] = {
        "request": request,
        "brand": BRAND_NAME,
        "url_base": settings.url_base,
        "current_user": user,
        # Derived here, not in the template: the nav shows it on every page and the
        # rule (first + last initial) is logic, not markup.
        "user_initials": display_initials(user.display_name) if user else "",
        "active_nav": active_nav,
        # Decided here so every page agrees, and so a logged-out page (login, the forced
        # password change) is dark whatever the account stores.
        "theme": user.theme if user else THEME_DARK,
        "asset_version": request.app.state.asset_version,
        "csrf_token": security.csrf_token_for(settings.session_secret, session_id),
        # What to call the media server anywhere a card mentions it. Base context, not
        # per-view, because two pages print it and neither should be able to disagree
        # with the Settings card about which server is connected. Reads the stored
        # connection, which is a small YAML file, and only for a signed-in page.
        "server_name": _media_server_name(request) if user else "",
    }
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context, status_code=status_code)


def _media_server_name(request: Request) -> str:
    """The connected server's display name, or empty when none is configured.

    Empty rather than a default: a page that says "Already in Plex" to someone running
    Jellyfin would be worse than one that says nothing, and no hint renders without a
    connection anyway.
    """
    stored = getattr(request.app.state, "media_server", None)
    connection = stored.load() if stored is not None else None
    return connection.name if connection is not None else ""


def display_initials(display_name: str) -> str:
    """Initials for the nav avatar: first letter of the first and last word.

    One word gives one letter. Sliced after upper-casing because some scripts expand on
    upper (German "ß" -> "SS"), and the disc has room for two characters.
    """
    words = display_name.split()
    if not words:
        return ""
    first = words[0][:1].upper()[:1]
    last = words[-1][:1].upper()[:1] if len(words) > 1 else ""
    return f"{first}{last}"


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def format_timestamp(moment: datetime | None) -> str:
    """Day-first date and time, matching how dates read everywhere else in the app."""
    if moment is None:
        return "—"
    return f"{moment.day}/{moment.month}/{moment.year} {moment:%H:%M}"


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp as written into the audit log; None when unparseable."""
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def optional_int(text: str) -> int | None:
    """Parse a form value into an int, or None when it isn't a plain integer."""
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def radarr_client_for(
    request: Request, app_id: str, *, timeout: float | None = None
) -> RadarrClient:
    """Build a Radarr client for a connection using the app's outbound-TLS settings.
    One place for the tls_verify / ca_file dance so the call sites can't drift."""
    settings = request.app.state.settings
    return request.app.state.apps.build_client(
        app_id,
        tls_verify=settings.outbound_tls_verify,
        ca_file=str(settings.tls_ca_file) if settings.tls_ca_file else None,
        timeout=timeout,
    )


def radarr_client_for_credentials(
    request: Request, url: str, api_key: str, *, timeout: float | None = None
) -> RadarrClient:
    """The same client `radarr_client_for` builds, for a connection not yet saved.

    Used by Test Connection in the Add form: nothing is stored, and the outbound-TLS
    settings are the app's own rather than anything the form can influence.
    """
    settings = request.app.state.settings
    return client_for_credentials(
        url,
        api_key,
        tls_verify=settings.outbound_tls_verify,
        ca_file=str(settings.tls_ca_file) if settings.tls_ca_file else None,
        timeout=timeout,
    )


async def cache_posters(request: Request, items: list[dict]) -> None:
    """Fetch each item's remote poster once into the local cache and set a same-origin
    `poster_local` URL (or None). Shared by the dashboard and the report detail.

    Distinct posters download concurrently, so first-view latency is the slowest single
    poster, not the sum of them all."""
    cache = request.app.state.posters
    url_base = request.app.state.settings.url_base
    # Every poster path routes through posters.sized() — the cache keys on the URL, so
    # fetching one form and pruning another would delete the whole cache.
    urls = {
        sized(item["poster_url"], POSTER_WIDTH) for item in items if item.get("poster_url")
    }
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[cache.ensure(client, url) for url in urls])
    for item in items:
        poster_url = sized(item.get("poster_url"), POSTER_WIDTH)
        if poster_url and cache.is_cached(poster_url):
            item["poster_local"] = f"{url_base}/posters/{cache.local_name(poster_url)}"
        else:
            item["poster_local"] = None


async def load_radarr_library(
    request: Request, app_id: str | None = None
) -> dict[int, RadarrMovie] | None:
    """A live snapshot of one Radarr's library keyed by tmdb_id, or None when
    unavailable (no app / unreachable). Defaults to the primary. Best-effort and bounded."""
    app_id = app_id or request.app.state.apps.primary_id()
    if app_id is None:
        return None
    if radarr_backoff(request).should_skip(app_id):
        # Same answer this call would have produced, without the wait. Callers already
        # treat None as "could not look" rather than "not there", so nothing downstream
        # has to know the difference.
        return None
    try:
        client = radarr_client_for(request, app_id, timeout=RADARR_LIBRARY_TIMEOUT_SECONDS)
        library = await asyncio.wait_for(
            client.list_movies(), timeout=RADARR_LIBRARY_TIMEOUT_SECONDS
        )
    except (RadarrError, TimeoutError, KeyError):
        radarr_backoff(request).note_failure(app_id)
        return None
    radarr_backoff(request).note_success(app_id)
    return {movie.tmdb_id: movie for movie in library}


MEDIA_SERVER_BACKOFF_KEY = "media-server"


async def load_media_server_snapshot(request: Request) -> MediaServerSnapshot | None:
    """What Plex holds, for annotating one page. None when unconfigured or unknowable.

    Reads the disk cache first: inside the TTL a render costs Plex nothing at all.
    Outside it, one bounded fetch refreshes the cache; a slow or down server falls
    back to the STALE snapshot rather than to nothing, because fifteen-minute-old
    truth about a library decorates a card better than silence — and the same backoff
    the Radarr reads use stops a dead server costing every render a timeout.
    """
    store = request.app.state.media_server
    if store.load() is None:
        return None
    cache = request.app.state.media_server_cache
    cached = cache.load()
    if cached is not None:
        snapshot, fetched_at = cached
        if time.time() - fetched_at < LIBRARY_CACHE_TTL_SECONDS:
            return snapshot
    stale = cached[0] if cached is not None else None
    backoff = request.app.state.media_server_backoff
    if backoff.should_skip(MEDIA_SERVER_BACKOFF_KEY):
        return stale
    settings = request.app.state.settings
    try:
        client = store.build_client(
            tls_verify=settings.outbound_tls_verify,
            ca_file=str(settings.tls_ca_file) if settings.tls_ca_file else None,
            timeout=RENDER_FETCH_TIMEOUT_SECONDS,
        )
        fetch = await asyncio.wait_for(
            client.list_movies(), timeout=RENDER_FETCH_TIMEOUT_SECONDS
        )
    except (MediaServerError, TimeoutError):
        backoff.note_failure(MEDIA_SERVER_BACKOFF_KEY)
        return stale
    backoff.note_success(MEDIA_SERVER_BACKOFF_KEY)
    cache.save(fetch)
    return snapshot_from_movies(fetch.movies, truncated=fetch.truncated)


async def load_radarr_options(request: Request, app_id: str | None = None) -> RadarrOptions:
    """Live profiles/folders from one Radarr, falling back to that connection's cache.

    Defaults to the primary. Best-effort and bounded — a slow or down Radarr yields the
    last-known options (or empty, which the templates render as plain text fields).
    """
    app_id = app_id or request.app.state.apps.primary_id()
    cache = request.app.state.radarr_options
    if app_id is None:
        return RadarrOptions()
    if radarr_backoff(request).should_skip(app_id):
        return cache.load(app_id)  # the same fallback the failure path returns
    try:
        client = radarr_client_for(request, app_id, timeout=RADARR_OPTIONS_TIMEOUT_SECONDS)
        options = await asyncio.wait_for(
            fetch_options(client), timeout=RADARR_OPTIONS_TIMEOUT_SECONDS
        )
    except (RadarrError, TimeoutError, KeyError):
        radarr_backoff(request).note_failure(app_id)
        return cache.load(app_id)
    radarr_backoff(request).note_success(app_id)
    # Only rewrite the cache when the options actually changed — otherwise every page
    # view churns radarr_options.yml under the global filestore write lock for nothing.
    if options != cache.load(app_id):
        cache.save(app_id, options)
    return options


async def load_all_radarr_options(request: Request) -> dict[str, RadarrOptions]:
    """Every connection's options, fetched concurrently.

    One slow instance costs the timeout, not the sum of them: the Settings page and the
    per-title target menu both need all of them at once.
    """
    apps = request.app.state.apps.list_apps()
    if not apps:
        return {}
    results = await asyncio.gather(
        *[load_radarr_options(request, app.id) for app in apps]
    )
    return dict(zip([app.id for app in apps], results, strict=True))


def cached_radarr_options(request: Request) -> dict[str, RadarrOptions]:
    """Every connection's last-known options, from disk, without asking any Radarr.

    For pages that need the Add menu but must not pay for it. `load_all_radarr_options`
    costs two requests per connection; the movie modal already spends a library lookup per
    connection plus a metadata call, and it is the fast-open path — a film the user is
    only reading about should not queue four more requests.

    Safe because the live answer is not load-bearing here. `effective_defaults` reads
    options only to reject a stored id against a NON-EMPTY list or to pick a first entry
    when nothing is stored, and a configured connection has both stored and vetted at save
    time. `ready` is a UI hint either way: `add_movie` fetches live options and resolves
    the pair again before it sends anything. An entry with nothing cached fails closed —
    disabled, saying to set a quality and folder — which is advice, not a wrong claim.
    """
    return request.app.state.radarr_options.load_all()


async def load_all_radarr_libraries(
    request: Request,
) -> dict[str, dict[int, RadarrMovie] | None]:
    """Every connection's library, concurrently. None for one that could not be read —
    the same "could not look" vs "not there" distinction the movie modal makes."""
    apps = request.app.state.apps.list_apps()
    if not apps:
        return {}
    results = await asyncio.gather(
        *[load_radarr_library(request, app.id) for app in apps]
    )
    return dict(zip([app.id for app in apps], results, strict=True))


async def load_radarr_queue(
    request: Request, app_id: str | None = None
) -> dict[int, float] | None:
    """One Radarr's download progress by movie id, or None when it could not be read.

    Deliberately the same shape as `load_radarr_library`: the same 4s bound, the same
    backoff, and None rather than an empty dict for a connection that did not answer. A
    page that cannot read the queue must render exactly as it did before there was one —
    an empty dict would mean "nothing is downloading", which is a different claim.
    """
    app_id = app_id or request.app.state.apps.primary_id()
    if app_id is None:
        return None
    if radarr_backoff(request).should_skip(app_id):
        return None
    try:
        client = radarr_client_for(request, app_id, timeout=RADARR_LIBRARY_TIMEOUT_SECONDS)
        return await asyncio.wait_for(
            client.queue(), timeout=RADARR_LIBRARY_TIMEOUT_SECONDS
        )
    except (RadarrError, TimeoutError, KeyError):
        radarr_backoff(request).note_failure(app_id)
        return None


async def load_all_radarr_queues(
    request: Request,
) -> dict[str, dict[int, float] | None]:
    """Every connection's queue, concurrently — the libraries' counterpart.

    Note what this does NOT do: it never calls `note_success`. The library read beside it
    already answers "is this box up", and a queue that failed on its own should not clear
    a backoff the library set.
    """
    apps = request.app.state.apps.list_apps()
    if not apps:
        return {}
    results = await asyncio.gather(*[load_radarr_queue(request, app.id) for app in apps])
    return dict(zip([app.id for app in apps], results, strict=True))


# The only schemes a link out of BoxMedia may carry.
ALLOWED_LINK_SCHEMES = ("http://", "https://")
# Browsers strip these from a URL before parsing it, so anything containing them is not
# the URL we validated.
_URL_CONTROL_CHARACTERS = ("\n", "\r", "\t", "\x00")


def safe_external_url(url: str | None) -> str | None:
    """A link only if it is plainly http(s), else nothing.

    Two values reach a template as an href from outside this app: the `website` a
    Radarr reports for a film, and the address of a Radarr connection itself.

    `website` is handed straight through from upstream metadata
    (`RadarrClient.movie_detail` reads Radarr's `website` field verbatim); the IMDb and
    trailer links are built server-side around a hardcoded `https://`, so their scheme
    cannot be influenced. The CSP already stops a `javascript:` href from executing —
    this is the layer that stops it being rendered as a link in the first place.

    Compared case-insensitively because URL schemes are, and stripped first, so a stray
    space in upstream data drops a legitimate link. Embedded control characters are
    refused outright: no real URL carries them, browsers strip them before parsing, and
    that gap between what we checked and what the browser sees is where smuggling lives.
    """
    if not url:
        return None
    candidate = url.strip()
    if any(character in candidate for character in _URL_CONTROL_CHARACTERS):
        return None
    return candidate if candidate.lower().startswith(ALLOWED_LINK_SCHEMES) else None


def radarr_url_for(app: ExternalApp, movie: RadarrMovie) -> str | None:
    """That film's page on that Radarr, or None when it cannot be addressed.

    `titleSlug` is what Radarr's own UI routes on, so this is the address the instance
    itself uses rather than a shape guessed at from the outside. A response without one —
    an older Radarr, or a movie record that arrived some other way — yields no link, and
    the chip stays the plain text it has always been.

    The base is the admin-configured connection address, never anything request-derived,
    and it goes through the same guard the film's `website` does before it becomes an
    href: `normalize_url` prepends http:// to anything without a scheme, so no stored
    value can be a `javascript:` URL, but it does let an embedded tab or newline through,
    and a browser strips those before parsing.
    """
    if not movie.title_slug:
        return None
    base = safe_external_url(app.url)
    return f"{base}/movie/{quote(movie.title_slug, safe='')}" if base else None


def _queued_percent(
    queues: dict[str, dict[int, float] | None] | None, app_id: str, movie: RadarrMovie
) -> float | None:
    """How far along this film's download is on this box, or None when there is no answer.

    None covers every case that is not a live percentage — no queue was fetched, that
    connection did not answer, or the film simply is not queued — because the chip renders
    identically for all of them, exactly as it did before there was a queue. A film that
    already has its file is never queued for that file, so it is not asked about.
    """
    if queues is None or movie.has_file or movie.radarr_id is None:
        return None
    queue = queues.get(app_id)
    return queue.get(movie.radarr_id) if queue else None


# How coarsely a download's progress is drawn. Ten steps, because a per-film percentage
# would have to be an inline `style` and the CSP is `style-src 'self'` — so the fill is one
# of eleven literal classes. Coarse enough for "is this nearly here", and the exact figure
# is still on the chip as a tooltip.
PROGRESS_STEP_PERCENT = 10


def progress_step(percent: float | None) -> int | None:
    """`42.0` -> `40`: the fill class this percentage draws as, or None for no fill."""
    if percent is None:
        return None
    return round(percent / PROGRESS_STEP_PERCENT) * PROGRESS_STEP_PERCENT


def progress_key(app_id: str, radarr_id: int | None) -> str | None:
    """How a progress indicator names itself to the poller, or None when it cannot.

    A film's download belongs to one connection AND one Radarr id — a Radarr id means
    nothing on a box that did not issue it — so both are in the key. The page carries it in
    a data attribute and `/progress` answers with the same shape, which is what lets one
    updater drive a chip, a card line and the modal without knowing which it is looking at.
    """
    return f"{app_id}:{radarr_id}" if radarr_id is not None else None


def radarr_locations(
    tmdb: int | None,
    libraries: dict[str, dict[int, RadarrMovie] | None],
    apps: dict[str, ExternalApp],
    queues: dict[str, dict[int, float] | None] | None = None,
) -> list[dict[str, object]]:
    """Where one film sits across the connections, and whether the file is really there.

    `has_file` is the whole point: a title queued on the 4K box is not "in library" on it,
    it is on its way. Both the dashboard chips and the weekly card's badge read this, so
    the two pages cannot disagree about one film.

    `radarr_url` is what turns naming the box into reaching it: for the one job BoxMedia
    deliberately leaves to Radarr — queue, files, history — the chip is the way there.

    `app_id`, `radarr_id` and `quality_profile_id` are what let an action target the
    connection that actually holds the film rather than the primary. All three are
    per-instance: a Radarr id means nothing on a box that did not issue it, and profile
    ids are per database — which is why the options cache is keyed by connection.
    """
    if tmdb is None:
        return []
    found: list[dict[str, object]] = []
    for app_id, library in libraries.items():
        movie = library.get(tmdb) if library is not None else None
        if movie is None:
            continue
        percent = _queued_percent(queues, app_id, movie)
        found.append(
            {"name": apps[app_id].name, "has_file": movie.has_file,
             "file_quality": movie.file_quality,
             "radarr_url": radarr_url_for(apps[app_id], movie),
             "app_id": app_id, "radarr_id": movie.radarr_id,
             "quality_profile_id": movie.quality_profile_id,
             "progress": percent, "progress_step": progress_step(percent),
             "progress_key": progress_key(app_id, movie.radarr_id)}
        )
    return found


def target_views(
    request: Request, options_by_app: dict[str, object]
) -> list[dict[str, object]]:
    """The Add menu: every connection, by the name the user gave it, primary first.

    `ready` is what decides whether an entry can be clicked — a connection with no
    resolvable quality or folder would only fail at the far end.
    """
    filters = request.app.state.filters.load()
    primary_id = request.app.state.apps.primary_id()
    targets: list[dict[str, object]] = []
    for app in request.app.state.apps.list_apps():
        options = options_by_app.get(app.id)
        profile_id, profile_name, folder = effective_defaults(app, options, filters)
        targets.append(
            {
                "id": app.id,
                "name": app.name,
                "primary": app.id == primary_id,
                "profile_id": profile_id,
                "profile_name": profile_name,
                "root_folder": folder,
                "ready": profile_id is not None and bool(folder),
            }
        )
    # The plain button adds to the primary, so the primary heads the menu it opens.
    targets.sort(key=lambda entry: not entry["primary"])
    return targets


def effective_defaults(
    app: object, options: object, filters: object
) -> tuple[int | None, str | None, str | None]:
    """What this connection adds as: its own setting, else the global Radarr Defaults.

    The global fallback keeps every pre-existing single-connection install working
    untouched — those users never set a per-connection default and should not have to.

    Both membership checks only reject against a NON-EMPTY list. An empty one means the
    live fetch failed and nothing was cached, not that the connection offers nothing —
    and a stored id was already vetted against this connection's own Radarr when it was
    saved. Discarding it during a blip turned a momentarily unreachable Radarr into
    "set a quality profile in Settings first" on a connection that already had one.
    """
    profiles = getattr(options, "profiles", []) or []
    folders = getattr(options, "root_folders", []) or []
    profile_id = app.quality_profile_id
    if profile_id is None or (
        profiles and all(profile.id != profile_id for profile in profiles)
    ):
        # Legacy fallback: the global pair predates per-connection defaults and is no
        # longer editable. Kept readable so an install that only ever set the old global
        # values keeps working until each connection is saved with its own.
        profile_id = filters.quality_profile_id
    if profile_id is not None and profiles and all(
        profile.id != profile_id for profile in profiles
    ):
        # The stored id belongs to a different instance's database; do not send it.
        profile_id = None
    if profile_id is None and profiles:
        profile_id = profiles[0].id
    name = next((profile.name for profile in profiles if profile.id == profile_id), None)

    folder = app.root_folder or filters.default_root_folder
    if folders and folder not in folders:
        folder = folders[0]
    return profile_id, name, folder
