"""Movie detail — one film's full record, without leaving BoxMedia.

Replaces the trip to IMDb/Wikipedia when deciding whether a chart title is worth
adding: overview, genres, runtime, certification, studio, and every rating source
Radarr carries, on one screen.

Served as a real page at `/movies/{tmdb_id}` and as a fragment of that same page at
`?fragment=1`. The poster links to the page, so the feature works with JavaScript off
and the detail is linkable; app.js intercepts the click and loads the fragment into a
`<dialog>`. One template either way, so the two can never drift.

Cast and crew appear only for movies already in a Radarr library — Radarr answers
/credit for an added movie and nothing else (see RadarrClient.credits). "a library"
means any connection, not the primary one: every box is asked, and the credits come
from whichever holds the film.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.services.posters import HEADSHOT_WIDTH, POSTER_WIDTH, sized
from app.services.radarr import CreditPerson, MovieDetail, RadarrError, RadarrMovie
from app.services.reports import imdb_url
from app.web.deps import (
    DETAIL_STATUS_MESSAGES,
    RADARR_LIBRARY_TIMEOUT_SECONDS,
    cached_radarr_options,
    current_user,
    load_all_radarr_queues,
    progress_key,
    radarr_backoff,
    radarr_client_for,
    radarr_locations,
    render,
    safe_external_url,
    target_views,
)

router = APIRouter()

MOVIE_PATH = "/movies/{tmdb_id}"
PROGRESS_PATH = "/progress"
FRAGMENT_PARAM = "fragment"
DETAIL_TIMEOUT_SECONDS = 8.0
# One modal must not trigger thirty headshot downloads. Radarr orders cast by billing,
# so the first names are the ones a viewer recognises.
MAX_CAST = 12
MAX_CREW = 6
# The crew list is long and mostly producers; these are the roles worth the space.
CREW_ROLES = ("Director", "Writer", "Screenplay", "Story", "Original Music Composer")
UNAVAILABLE_MESSAGE = (
    "Radarr could not be reached, so details for this film are unavailable right now."
)
NO_CAST_MESSAGE = (
    "Radarr only provides cast and crew for films already in your library. "
    "Add this one and the full credits appear here."
)
# Said whenever credits are missing for a reason that is NOT "you don't own this film":
# a library lookup failed, or the credits call did. The metadata lookup gets a longer
# budget than the per-connection library lookup, so a big or slow Radarr can answer one
# and not the other; and with several connections, one silent box is enough. Claiming
# "not in your library" in either case would be a lie about the user's own collection.
CAST_UNAVAILABLE_MESSAGE = (
    "Cast and crew could not be loaded from Radarr just now. "
    "Everything else here is up to date."
)


def _minutes_display(minutes: int | None) -> str | None:
    """167 -> '2h 47m'. Digit-grouped units, not a locale-specific date format."""
    if not minutes:
        return None
    hours, remainder = divmod(minutes, 60)
    if not hours:
        return f"{remainder}m"
    return f"{hours}h {remainder}m" if remainder else f"{hours}h"


def _pick_crew(crew: tuple[CreditPerson, ...]) -> list[CreditPerson]:
    """Head-of-department roles first, de-duplicated by person+role."""
    seen: set[tuple[str, str | None]] = set()
    picked: list[CreditPerson] = []
    for role in CREW_ROLES:
        for person in crew:
            key = (person.name, person.role)
            if person.role != role or key in seen:
                continue
            seen.add(key)
            picked.append(person)
    return picked[:MAX_CREW]


async def _cache_headshots(request: Request, people: list[CreditPerson]) -> list[dict]:
    """Proxy each headshot through the local poster cache so `img-src 'self'` holds.

    Downloads run concurrently, so the wait is the slowest single image rather than
    the sum, and the cache means a second open of the same film fetches nothing.
    """
    cache = request.app.state.posters
    url_base = request.app.state.settings.url_base
    urls = {
        sized(person.headshot_url, HEADSHOT_WIDTH) for person in people if person.headshot_url
    }
    if urls:
        async with httpx.AsyncClient() as client:
            await asyncio.gather(*[cache.ensure(client, url) for url in urls])
    views = []
    for person in people:
        headshot_url = sized(person.headshot_url, HEADSHOT_WIDTH)
        cached = headshot_url and cache.is_cached(headshot_url)
        views.append(
            {
                "name": person.name,
                "role": person.role,
                "headshot": (
                    f"{url_base}/posters/{cache.local_name(headshot_url)}" if cached else None
                ),
            }
        )
    return views


async def _poster_local(request: Request, url: str | None) -> str | None:
    """The modal poster, at the same size the grids use so both share one cache entry."""
    url = sized(url, POSTER_WIDTH)
    if not url:
        return None
    cache = request.app.state.posters
    async with httpx.AsyncClient() as client:
        await cache.ensure(client, url)
    if not cache.is_cached(url):
        return None
    return f"{request.app.state.settings.url_base}/posters/{cache.local_name(url)}"


def _facts(detail: MovieDetail) -> list[dict[str, str]]:
    """The labelled one-liners under the title, skipping anything Radarr didn't report."""
    candidates = (
        ("Runtime", _minutes_display(detail.runtime_minutes)),
        ("Rated", detail.certification),
        ("Studio", detail.studio),
        ("Language", detail.original_language),
        ("Status", detail.status.title() if detail.status else None),
    )
    return [{"label": label, "value": value} for label, value in candidates if value]


def _no_cast_reason(
    library_known: bool, in_library: object | None, cast_views: list[dict]
) -> str | None:
    """Why the credits are absent, or None when they are not.

    Only one case may blame the user's library: we positively read EVERY library and the
    film was in none of them. Everything else — a library unreadable, credits call
    failed, a library film Radarr has no credits for — is our side failing, and says so.
    """
    if cast_views:
        return None
    if library_known and in_library is None:
        return NO_CAST_MESSAGE
    return CAST_UNAVAILABLE_MESSAGE


async def _holder_of(
    request: Request, app_id: str, tmdb_id: int
) -> tuple[str, RadarrMovie | None, bool]:
    """(connection, the film if this box holds it, whether the box answered at all).

    The targeted `/movie?tmdbId=` lookup rather than a whole-library pull: this runs once
    per connection on every modal open, and the answer is about a single film.

    Bounded by the library budget, not the detail one. Every open now waits on every
    connection, so a box that is down must not hold the dialog for the longer budget the
    metadata call gets — and this is the same wait the dashboard allows for a library read.

    A connection that just failed is skipped entirely and reported as not having answered,
    which is the honest word for it: the modal already refuses to say "not in your library"
    unless every box replied, so a skipped one keeps that message off the screen rather
    than turning a backoff into a claim about the user's collection.
    """
    backoff = radarr_backoff(request)
    if backoff.should_skip(app_id):
        return app_id, None, False
    try:
        client = radarr_client_for(request, app_id, timeout=RADARR_LIBRARY_TIMEOUT_SECONDS)
        movie = await asyncio.wait_for(
            client.movie_by_tmdb(tmdb_id), timeout=RADARR_LIBRARY_TIMEOUT_SECONDS
        )
    except (RadarrError, TimeoutError, KeyError):
        backoff.note_failure(app_id)
        return app_id, None, False
    backoff.note_success(app_id)
    return app_id, movie, True


def _detail_source(primary_id: str, holder_id: str | None, answered: list[str]) -> str:
    """Which connection is asked for the film's metadata.

    Any Radarr can answer a TMDB lookup for any film, so this is only about picking one
    that is actually up. The primary first, so a working setup behaves exactly as before;
    otherwise the box holding the film, and failing that whichever one replied — a down
    primary should not blank a modal another connection can fill.

    When NOTHING answered the library lookup we still ask: that is a different endpoint,
    and a Radarr slow enough to miss one call can serve the next. Losing the whole record
    over it would hide a film's details because we could not read a library.
    """
    if primary_id in answered:
        return primary_id
    if holder_id is not None:
        return holder_id
    if answered:
        return answered[0]
    return primary_id


@router.get(MOVIE_PATH)
async def movie_detail(request: Request, tmdb_id: int) -> HTMLResponse:
    """One film's record. `?fragment=1` returns the inner block for the modal."""
    current_user(request)
    fragment = request.query_params.get(FRAGMENT_PARAM) == "1"
    template = "_movie_detail.html" if fragment else "movie_detail.html"

    apps = request.app.state.apps.list_apps()
    if not apps:
        return render(request, template, movie=None, error=UNAVAILABLE_MESSAGE)

    # Every connection at once, not just the primary: a film added to the second box is
    # in the user's library, and its credits live on the box that holds it.
    # The queues ride in the same gather, so how far a download has got costs the slowest
    # single request rather than a second round. F13 left this page out to keep the modal
    # cheap to open; asked for everywhere, it belongs here too — this is the page opened
    # from the dashboard, from a weekly card, and from the month leaderboard alike.
    lookups, queues = await asyncio.gather(
        asyncio.gather(*[_holder_of(request, app.id, tmdb_id) for app in apps]),
        load_all_radarr_queues(request),
    )
    answered = [app_id for app_id, _, replied in lookups if replied]
    holder_id, in_library = next(
        ((app_id, movie) for app_id, movie, _ in lookups if movie is not None), (None, None)
    )
    # "Not in your library" is only honest once every box has answered. With one silent,
    # the film may well be sitting on it, so the message stays on our side of the fence.
    library_known = len(answered) == len(apps)

    # Which boxes hold it, in the shape the dashboard and the search results already use,
    # so "In Library · Main" cannot end up worded differently on three pages. A connection
    # that never replied stays None rather than an empty library — the same "could not
    # look" versus "not there" distinction the rest of this function makes.
    apps_by_id = {app.id: app for app in apps}
    holders = radarr_locations(
        tmdb_id,
        {
            app_id: ({tmdb_id: movie} if movie is not None else ({} if replied else None))
            for app_id, movie, replied in lookups
        },
        apps_by_id,
        queues,
    )

    try:
        detail_client = radarr_client_for(
            request,
            # primary_id() is None only when there are no connections, ruled out above.
            _detail_source(request.app.state.apps.primary_id() or apps[0].id, holder_id, answered),
            timeout=DETAIL_TIMEOUT_SECONDS,
        )
        detail = await asyncio.wait_for(
            detail_client.movie_detail(tmdb_id), timeout=DETAIL_TIMEOUT_SECONDS
        )
    except (RadarrError, TimeoutError, KeyError):
        return render(request, template, movie=None, error=UNAVAILABLE_MESSAGE)

    cast_views: list[dict] = []
    crew_views: list[dict] = []
    if in_library is not None and in_library.radarr_id is not None:
        try:
            # The holder's own client and the holder's own id — a Radarr id means nothing
            # on a box that did not issue it.
            holder_client = radarr_client_for(
                request, holder_id, timeout=DETAIL_TIMEOUT_SECONDS
            )
            cast, crew = await asyncio.wait_for(
                holder_client.credits(in_library.radarr_id), timeout=DETAIL_TIMEOUT_SECONDS
            )
            cast_views = await _cache_headshots(request, list(cast[:MAX_CAST]))
            crew_views = await _cache_headshots(request, _pick_crew(crew))
        except (RadarrError, TimeoutError, KeyError):
            pass  # the rest of the record is still worth showing

    # The Add menu, from cache — the modal is a read that must open fast, and the live
    # fetch is not load-bearing here (see cached_radarr_options).
    targets = target_views(request, cached_radarr_options(request))
    # An add made from this page returns to it, so this is where it explains itself.
    banner = DETAIL_STATUS_MESSAGES.get(request.query_params.get("status", ""))

    return render(
        request,
        template,
        targets=targets,
        # The split button only earns its caret when there is somewhere else to send to.
        show_targets=len(targets) > 1,
        banner_kind=banner[0] if banner else None,
        banner_text=banner[1] if banner else None,
        movie={
            "tmdb_id": detail.tmdb_id,
            "title": detail.title,
            "year": detail.year,
            "overview": detail.overview,
            "genres": detail.genres,
            "ratings": detail.ratings,
            "facts": _facts(detail),
            "poster_local": await _poster_local(request, detail.poster_url),
            # The shared builder, not a third copy of the format: it already returns None
            # for a missing id, and it exists so every IMDb link in the app agrees.
            "imdb_url": imdb_url(detail.imdb_id),
            "trailer_url": (
                f"https://www.youtube.com/watch?v={detail.trailer_id}"
                if detail.trailer_id
                else None
            ),
            "website": safe_external_url(detail.website),
            "in_library": in_library is not None,
            "holders": holders,
            "cast": cast_views,
            "crew": crew_views,
            "no_cast_reason": _no_cast_reason(library_known, in_library, cast_views),
        },
        error=None,
    )


@router.get(PROGRESS_PATH)
async def progress(request: Request) -> JSONResponse:
    """What every connection is downloading right now, keyed the way the pages are.

    The one thing a rendered page cannot do for itself: a download that was 40% when the
    page was built is not 40% a minute later, and until now the only way to find out was
    to reload. app.js polls this and rewrites the indicators in place.

    Read-only, behind the same session gate as every other route, and it exposes nothing a
    page does not already render — the percentages ARE what the chips and lines show. No
    titles, no paths, no ids beyond the ones the markup already carries.

    A connection that cannot be read contributes nothing rather than zeroes, so a Radarr
    that went away leaves the last-known figures on screen instead of resetting them all
    to "just started".
    """
    current_user(request)
    queues = await load_all_radarr_queues(request)
    live = {
        key: percent
        for app_id, queue in queues.items()
        if queue
        for radarr_id, percent in queue.items()
        if (key := progress_key(app_id, radarr_id)) is not None
    }
    # no-store: a percentage is stale the moment it is cached, and a proxy replaying one
    # would make the page confidently wrong rather than merely out of date.
    return JSONResponse(live, headers={"Cache-Control": "no-store"})
