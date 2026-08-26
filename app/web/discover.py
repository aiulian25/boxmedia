"""Discover — what is doing well this week, in cinemas and on television (TV step 11).

The direction's honest page. Films are ranked by what they took at the box office and
series by how many people are watching them on Trakt right now, and the page **says so**
rather than implying a parity that does not exist: there is no television box office.

## Nothing here reaches the network

Every row renders from something already on disk:

* the film shelf from the latest stored report — zero fetches, and it is the same data
  the weekly view already shows;
* the two Trakt shelves from `DiscoverCache`.

External calls live in the Refresh action alone: session-gated, CSRF-guarded, audited,
and bounded. That is the difference between a slow shelf and a slow app — a page whose
render can be held open by a third party having a bad day is a page that goes down when
they do.

## State is resolved here, not stored

The cache holds what came off the network and nothing else. Whether a show is already in
Sonarr, or sitting on your media server, is answered at RENDER against the snapshots
steps 8 and 9 built. Storing the verdict would have been a bug you could see: add a
series to Sonarr and its card would keep saying "Wanted" until the shelf refreshed six
hours later.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi import status as http_status
from fastapi.responses import RedirectResponse

from app.core.audit import AuditAction
from app.services.boxoffice import week_start
from app.services.discovery import (
    ANTICIPATED_KEY,
    DISCOVER_ROW_SIZE,
    PROVIDER_TRAKT,
    TRENDING_KEY,
    DiscoverShow,
)
from app.services.mediaserver import HOLDS_PROBABLY, HOLDS_YES
from app.services.posters import SERIES_POSTER_WIDTH
from app.services.reports import MovieStatus, RunStatus
from app.services.trakt import TraktClient, TraktError
from app.web.deps import (
    MEDIA_TYPES,
    TYPE_ALL,
    TYPE_MOVIES,
    TYPE_TV,
    cache_posters,
    client_ip,
    current_user,
    render,
    validated_media_type,
)
from app.web.profile import STATUS_QUERY_KEY

router = APIRouter()

NAV_KEY = "discover"
DISCOVER_PATH = "/discover"
REFRESH_PATH = "/discover/refresh"

# How long the Refresh action may hold the request open. Past this the answer is "that
# took too long", which is true and actionable, rather than a spinner nobody can cancel.
REFRESH_TIMEOUT_SECONDS = 12.0

# How a card reads. Four states, and each one is a different next action: open it where
# it already is, add it, verify a guess, or find it by hand because nothing can add it.
STATE_IN_SONARR = "in_sonarr"
STATE_ON_SERVER = "on_server"
STATE_MAYBE_ON_SERVER = "maybe_on_server"
STATE_WANTED = "wanted"
STATE_NO_TVDB = "no_tvdb"


def _film_views(request: Request) -> tuple[list[dict], str | None]:
    """The chart shelf, from the latest COMPLETED report. No fetches.

    Returns the cards and the week they came from. A report-less install gets an empty
    shelf and a caption saying so — the television rows still render, which is the whole
    point of the two halves being independent.
    """
    reports = request.app.state.reports.list_reports()
    latest = next((report for report in reports if report.status == RunStatus.OK), None)
    if latest is None:
        return [], None
    views = []
    for movie in latest.movies[:DISCOVER_ROW_SIZE]:
        views.append({
            "kind": TYPE_MOVIES,
            "title": movie.title,
            "rank": movie.rank,
            "meta": f"{movie.gross_display} · Wk {movie.weeks_in_release}",
            "facts": _facts(movie.rating, movie.genres),
            "poster_url": movie.poster_url,
            "tmdb_id": movie.tmdb_id,
            "imdb_url": movie.imdb_url,
            # The film half already has a home for deciding — the weekly report, with its
            # add control, its fix-match and its ignore. Sending people there beats
            # growing a second, thinner copy of all three here, so a film card links to
            # its week rather than carrying its own Add.
            "state": (
                STATE_IN_SONARR
                if movie.status == MovieStatus.IN_LIBRARY
                else STATE_WANTED
            ),
        })
    return views, latest.week


def _facts(rating: float | None, genres: list[str] | tuple[str, ...]) -> str:
    """The one line under a card: what it is, and what people think of it.

    Built here rather than in the template because it is a rule (star only when rated,
    separator only when both), and the template's job is to place it, not to decide it.
    """
    parts = []
    if rating:
        parts.append(f"★ {rating:.1f}")
    if genres:
        parts.append(" · ".join(genres))
    return " · ".join(parts)


def _show_views(
    request: Request, shows: tuple[DiscoverShow, ...], *, count_label: str
) -> list[dict]:
    """Trakt rows resolved against what you already hold.

    Resolved HERE, from snapshots, so a series added a minute ago reads correctly without
    waiting for the shelf's six-hour TTL. Sonarr answers first: it is the thing that can
    actually fetch the show, and "already in Sonarr" is a stronger statement than "your
    media server has it".
    """
    series = request.app.state.series_cache.snapshot()
    server = request.app.state.media_server
    stored_server = server.load()
    server_snapshot = None
    if stored_server is not None:
        cached = request.app.state.media_server_cache.load()
        server_snapshot = cached[0] if cached else None
    server_name = stored_server.name if stored_server else None

    views = []
    for show in shows:
        held = series.find(tvdb_id=show.tvdb_id, title=show.title, year=show.year)
        on_server = (
            server_snapshot.holds_series(
                show.tvdb_id, show.tmdb_id, show.imdb_id, show.title, show.year
            )
            if server_snapshot is not None
            else None
        )
        views.append({
            "kind": TYPE_TV,
            "title": show.title,
            "meta": _show_meta(show, count_label),
            "facts": _facts(show.rating, ()),
            "poster_url": show.poster_url,
            "tmdb_id": show.tmdb_id,
            "tvdb_id": show.tvdb_id,
            "imdb_url": f"https://www.imdb.com/title/{show.imdb_id}/" if show.imdb_id else None,
            "server_name": server_name,
            **_show_state(show, held, on_server),
        })
    return views


def _show_meta(show: DiscoverShow, count_label: str) -> str:
    """Year, and the number this row is ranked by — named, because `watchers` and
    `list_count` measure different things and a bare figure would mean whichever the
    reader assumed."""
    parts = [str(show.year)] if show.year else []
    figure = show.watchers if show.watchers is not None else show.list_count
    if figure is not None:
        parts.append(f"{figure:,} {count_label}")
    return " · ".join(parts)


def _show_state(show: DiscoverShow, held: object, on_server: str | None) -> dict:
    """What this card says, and what it offers next.

    The ladder is deliberate. Already in Sonarr wins outright — it is the answer that
    changes what you would do. Then "your server has it, Sonarr does not", which is the
    mockup's most valuable state and the reason the show library exists. Then the honest
    refusal for a show nothing can add. Then plain wanted.
    """
    if held is not None:
        missing = held.series.missing_episode_count
        return {
            "state": STATE_IN_SONARR,
            "state_text": (
                f"Complete — {held.series.episode_count} episodes"
                if held.series.complete
                else f"Missing {missing} episode{'s' if missing != 1 else ''}"
            ),
            "guess": held.state == HOLDS_PROBABLY,
        }
    if on_server == HOLDS_YES:
        return {"state": STATE_ON_SERVER, "state_text": None, "guess": False}
    if on_server == HOLDS_PROBABLY:
        return {"state": STATE_MAYBE_ON_SERVER, "state_text": None, "guess": True}
    if not show.addable:
        return {
            "state": STATE_NO_TVDB,
            # Said plainly rather than as a failed Add: Sonarr keys series on TVDB ids,
            # and TMDB has none for this show, so no button here could work.
            "state_text": "No TVDB id — search Sonarr by name",
            "guess": False,
        }
    return {"state": STATE_WANTED, "state_text": None, "guess": False}


@router.get(DISCOVER_PATH)
async def discover(request: Request, type: str = TYPE_ALL) -> object:  # noqa: A002
    """The page. Renders from disk; makes no outbound request of any kind."""
    current_user(request)
    media_type = validated_media_type(type)
    keys = request.app.state.discovery.load()
    cache = request.app.state.discover_cache
    rows = cache.load()

    films, week = _film_views(request) if media_type != TYPE_TV else ([], None)
    show_rows = []
    if media_type != TYPE_MOVIES:
        show_rows = [
            {
                "id": TRENDING_KEY,
                "title": "Trending now",
                "source": "Trakt · artwork from TMDB",
                "cards": _show_views(
                    request, rows[TRENDING_KEY], count_label="watching"
                ),
            },
            {
                "id": ANTICIPATED_KEY,
                "title": "Anticipated",
                "source": "Trakt · not aired yet",
                "cards": _show_views(
                    request, rows[ANTICIPATED_KEY], count_label="waiting"
                ),
            },
        ]

    # One pass over every card on the page, so the posters download concurrently rather
    # than shelf by shelf. Series at their own width — see posters.SERIES_POSTER_WIDTH.
    await cache_posters(request, films)
    await cache_posters(
        request,
        [card for row in show_rows for card in row["cards"]],
        width=SERIES_POSTER_WIDTH,
    )

    banner = STATUS_MESSAGES.get(request.query_params.get(STATUS_QUERY_KEY, ""))
    return render(
        request,
        "discover.html",
        active_nav=NAV_KEY,
        banner_kind=banner[0] if banner else None,
        banner_text=banner[1] if banner else None,
        media_type=media_type,
        media_types=MEDIA_TYPES,
        discover_path=DISCOVER_PATH,
        refresh_path=REFRESH_PATH,
        keys=keys.public(),
        films=films,
        film_week=week,
        film_week_date=_week_display(week),
        show_rows=show_rows,
        fetched_at=cache.fetched_at(),
        stale=cache.is_stale(),
    )


def _week_display(week: str | None) -> str | None:
    """The week's own start date, day-first — never American, and never re-derived from
    a second copy of the ISO-calendar arithmetic."""
    start = week_start(week) if week else None
    return f"{start.day}/{start.month}/{start.year}" if start else None


@router.post(REFRESH_PATH)
async def refresh(request: Request) -> RedirectResponse:
    """Fetch both Trakt shelves. The ONLY outbound call this feature makes on demand.

    Session-gated and CSRF-guarded by the router's dependency, audited, and bounded — so
    the worst a slow Trakt can do is make this one action take twelve seconds, never a
    page render.

    A failure leaves the previous shelves in place: "we could not look" and "there is
    nothing" are different claims, and only one of them is true.
    """
    user = current_user(request)
    client_id = request.app.state.discovery.decrypt(PROVIDER_TRAKT)
    if not client_id:
        return _redirect(request, DiscoverStatus.NO_KEYS)
    try:
        rows = await asyncio.wait_for(
            _fetch_rows(client_id), timeout=REFRESH_TIMEOUT_SECONDS
        )
    except (TraktError, TimeoutError):
        return _redirect(request, DiscoverStatus.REFRESH_FAILED)
    request.app.state.discover_cache.save(rows)
    request.app.state.audit.record(
        AuditAction.DISCOVER_REFRESHED,
        actor=user.username,
        source_ip=client_ip(request),
        trending=len(rows[TRENDING_KEY]),
        anticipated=len(rows[ANTICIPATED_KEY]),
    )
    return _redirect(request, DiscoverStatus.REFRESHED)


async def _fetch_rows(client_id: str) -> dict[str, tuple[DiscoverShow, ...]]:
    """Both shelves, concurrently — one slow row costs the timeout, not the sum."""
    client = TraktClient(client_id)
    trending, anticipated = await asyncio.gather(
        client.trending_shows(DISCOVER_ROW_SIZE),
        client.anticipated_shows(DISCOVER_ROW_SIZE),
    )
    return {
        TRENDING_KEY: tuple(_from_trakt(show) for show in trending),
        ANTICIPATED_KEY: tuple(_from_trakt(show) for show in anticipated),
    }


def _from_trakt(show: object) -> DiscoverShow:
    """A Trakt row as the shelf stores it.

    No poster: Trakt returns ids and titles and never artwork. TMDB fills that in — and
    deliberately not here, because a shelf that renders without pictures is still a
    usable shelf, while one that fails to build because an image host is down is not.
    """
    return DiscoverShow(
        tmdb_id=show.tmdb_id,
        tvdb_id=show.tvdb_id,
        imdb_id=show.imdb_id,
        trakt_id=show.trakt_id,
        title=show.title,
        year=show.year,
        overview=show.overview,
        poster_url=None,
        watchers=show.watchers,
        list_count=show.list_count,
    )


class DiscoverStatus:
    """Why the page reloaded, as a closed enum — never text from anywhere else."""

    REFRESHED = "discover_refreshed"
    REFRESH_FAILED = "discover_refresh_failed"
    NO_KEYS = "discover_no_keys"


STATUS_MESSAGES = {
    DiscoverStatus.REFRESHED: ("success", "Discover refreshed."),
    DiscoverStatus.REFRESH_FAILED: (
        "error",
        "Could not reach Trakt — the shelves below are the last ones fetched.",
    ),
    DiscoverStatus.NO_KEYS: (
        "error",
        "Add a Trakt client ID in Settings first.",
    ),
}


def _redirect(request: Request, status_code: str) -> RedirectResponse:
    base = request.app.state.settings.url_base
    return RedirectResponse(
        f"{base}{DISCOVER_PATH}?{STATUS_QUERY_KEY}={status_code}",
        status_code=http_status.HTTP_303_SEE_OTHER,
    )
