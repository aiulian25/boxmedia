"""Box Office dashboard — the library view.

Shows only titles that are actually in Radarr: In Library (downloaded) or Wanted
(added, awaiting download) — whether added via BoxMedia's weekly view or already
sitting in Radarr. Adding new titles is a deliberate action on the weekly report,
not here. Status is recomputed against a live Radarr snapshot, with the stored
status as fallback. Posters are served locally so the CSP can stay `img-src 'self'`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.core.sessions import COOKIE_NAME
from app.services.apps import ExternalApp
from app.services.boxoffice import DEFAULT_CURRENCY_SYMBOL, format_gross
from app.services.matcher import normalize_title
from app.services.radarr import RadarrMovie
from app.services.reports import MovieStatus, Report, RunStatus, imdb_url, wiki_url
from app.web.deps import (
    cache_posters,
    current_user,
    format_timestamp,
    load_all_radarr_libraries,
    load_all_radarr_queues,
    load_media_server_snapshot,
    parse_timestamp,
    radarr_locations,
    render,
)

router = APIRouter()

NAV_KEY = "dashboard"
DASHBOARD_PATH = "/dashboard"
# The page is a scrollable grid, so paging every 10 made the reader click for something
# scrolling already gives them. The cap exists only to bound the poster fetches and the
# markup for a very large library — most libraries never reach it and never see the link.
DEFAULT_LIMIT = 100
PAGE_INCREMENT = 100
LIBRARY_STATUSES = (MovieStatus.IN_LIBRARY, MovieStatus.WANTED)


def _merge_history(reports: list[Report]) -> list[dict]:
    """Newest occurrence of each title wins (reports are newest-first).

    The running total is the exception: it is folded across every sighting rather than
    taken from the newest one. `list_reports` orders by when a report was WRITTEN, so
    re-running an older week — which is how a week's figures get better — puts that week
    at the front of the list, and reading the total from there would quietly replace the
    film's lifetime gross with a staler, smaller one. A running total only ever grows, so
    the largest seen is the current one. Same fold, same reason, as the month leaderboard.
    """
    by_title: dict[str, dict] = {}
    for report in reports:
        if report.status != RunStatus.OK:
            continue
        for movie in report.movies:
            entry = by_title.setdefault(
                movie.normalized_title,
                {
                    "title": movie.title,
                    "normalized_title": movie.normalized_title,
                    "status": movie.status,
                    "gross_display": movie.gross_display,
                    "weeks_in_release": movie.weeks_in_release,
                    "tmdb_id": movie.tmdb_id,
                    "year": movie.year,
                    "poster_url": movie.poster_url,
                    "imdb_url": movie.imdb_url,
                    "wiki_url": movie.wiki_url,
                    "total_gross": None,
                    # The freshest sighting decides which money this card speaks. Every
                    # figure on it is then computed against that one, so the tracked sum
                    # and the lifetime figure on the same line can never be in different
                    # currencies.
                    "currency": report.currency,
                },
            )
            # Only fold a running total that is in the SAME money. A max across currencies
            # is not a bigger number, it is a meaningless one — and after the region became
            # a setting, a history really can hold both.
            if movie.total_gross is not None and report.currency == entry["currency"]:
                entry["total_gross"] = max(entry["total_gross"] or 0, movie.total_gross)
    # Insertion order is first-sighting order, which is what the grid showed before this
    # became a dict: newest week first, chart rank within it.
    return list(by_title.values())


def _library_only_views(
    charted: list[dict],
    libraries: dict[str, dict[int, RadarrMovie] | None],
) -> list[dict]:
    """Cards for the titles Radarr holds that no stored report ever covered.

    The page calls itself "titles in Radarr, added here or already there", but it was
    built purely from report history — so a film added before BoxMedia existed, or during
    a week it never scraped, was simply absent. Someone looking for it concluded they had
    never added it and went hunting through the weekly reports again.

    Everything here comes from the library snapshot already in hand, so no extra request
    is made. Box-office figures are genuinely unknown for these titles rather than zero,
    and are left as None for the template to skip.
    """
    known = {movie["tmdb_id"] for movie in charted if movie["tmdb_id"] is not None}
    seen: set[int] = set()
    extras: list[dict] = []
    for library in libraries.values():
        for tmdb, movie in (library or {}).items():
            if tmdb in known or tmdb in seen:
                continue
            seen.add(tmdb)
            extras.append(
                {
                    "title": movie.title,
                    "normalized_title": normalize_title(movie.title),
                    "status": MovieStatus.IN_LIBRARY if movie.has_file else MovieStatus.WANTED,
                    "gross_display": None,  # never charted in a week we hold
                    "weeks_in_release": None,
                    "total_gross": None,
                    "currency": DEFAULT_CURRENCY_SYMBOL,
                    "tmdb_id": tmdb,
                    "year": movie.year,
                    "poster_url": movie.poster_url,
                    "imdb_url": imdb_url(movie.imdb_id),
                    "wiki_url": wiki_url(movie.title),
                }
            )
    # Alphabetical, so the tail of the grid is predictable to scan. The charted titles
    # keep their own order ahead of these — newest week first, chart rank within it.
    extras.sort(key=lambda movie: movie["title"].casefold())
    return extras


def _apply_locations(
    movies: list[dict],
    libraries: dict[str, dict[int, RadarrMovie] | None],
    apps_by_id: dict[str, ExternalApp],
    queues: dict[str, dict[int, float] | None],
) -> None:
    """Annotate each title with the connections holding it, and derive its live status.

    A title is In Library once ANY connection has the file; until then it is Wanted, and
    the chips say which box it is heading for.
    """
    for movie in movies:
        locations = radarr_locations(movie["tmdb_id"], libraries, apps_by_id, queues)
        movie["locations"] = locations
        if not locations:
            continue  # nothing that answered has it — leave the stored status alone
        downloaded = [entry for entry in locations if entry["has_file"]]
        movie["status"] = MovieStatus.IN_LIBRARY if downloaded else MovieStatus.WANTED
        # One holder: the badge can name the quality. Several: there is no single quality
        # to name, and the chips already say where each copy is.
        movie["file_quality"] = downloaded[0]["file_quality"] if len(downloaded) == 1 else None


def _sign_in_notice_view(request: Request) -> dict | None:
    """The one-shot 'last sign-in' notice, read (and cleared) from the session."""
    notice = request.app.state.sessions.pop_notice(request.cookies.get(COOKIE_NAME))
    if notice is None:
        return None
    return {
        "at": format_timestamp(parse_timestamp(notice.get("at"))),
        "ip": notice.get("ip") or "an unknown address",
        "failed": notice.get("failed") or 0,
    }


def _apply_tracking(
    movies: list[dict], histories: dict[str, list[tuple[str, int, int, str]]]
) -> None:
    """Annotate each library title with the two box-office figures its card can show.

    Both are reported in ONE currency — the freshest sighting's — and weeks in any other
    are left out of the arithmetic rather than converted, because this app knows no
    exchange rate and inventing one would be worse than saying less.

    They are different measurements and the card names both: the tracked sum covers only
    the weeks this install actually holds, while the lifetime figure is what Box Office
    Mojo reports for the film's whole run. A film picked up in week 7 of 9 has a tracked
    sum that is a fraction of its real take, which is precisely what one unlabelled
    "total" used to claim. Lifetime is None whenever no stored report carries one — for
    every title Radarr holds that no week ever charted, and for reports written before
    the scraper read Mojo's Total Gross column.
    """
    for movie in movies:
        history = histories.get(movie["normalized_title"], [])
        currency = movie["currency"]
        movie["weeks_tracked"] = len(history)
        # Only the weeks in this card's own currency. A history that mixes them is
        # possible now that the region is a setting, and adding pounds to dollars would
        # not be a formatting slip — it would be a number that means nothing, printed with
        # the confidence of one that does.
        movie["gross_total_display"] = format_gross(
            sum(gross for _, _, gross, money in history if money == currency), currency
        )
        movie["lifetime_display"] = (
            format_gross(movie["total_gross"], currency) if movie["total_gross"] else None
        )


@router.get(DASHBOARD_PATH)
async def dashboard(request: Request, q: str = "", limit: int = DEFAULT_LIMIT) -> object:
    current_user(request)
    sign_in_notice = _sign_in_notice_view(request)
    reports = request.app.state.reports.list_reports()

    movies = _merge_history(reports)
    # Every connection, not just the primary: a title sent to the 4K box belongs on this
    # page as much as one on the main instance. The queues ride along in the same gather,
    # so live progress costs the slowest single request rather than a second round.
    libraries, queues = await asyncio.gather(
        load_all_radarr_libraries(request), load_all_radarr_queues(request)
    )
    apps_by_id = {app.id: app for app in request.app.state.apps.list_apps()}
    answered = {app_id: lib for app_id, lib in libraries.items() if lib is not None}
    # Everything else Radarr holds, appended after the charted titles. Built from the
    # snapshot already fetched above, so this costs no extra request.
    movies = movies + _library_only_views(movies, answered)
    # One read of the history for the whole grid, so "3 wks tracked" can never disagree
    # with the trend line on the weekly view.
    _apply_tracking(movies, request.app.state.reports.histories(reports))
    _apply_locations(movies, answered, apps_by_id, queues)
    if answered and len(answered) == len(libraries):
        # Only authoritative once EVERY box answered: a title none of them has was deleted
        # in Radarr and should drop off. With one silent, a title living only on that box
        # would otherwise vanish from the page it belongs on.
        movies = [movie for movie in movies if movie["locations"]]

    # Library view: only titles that are actually in Radarr (stored-status fallback
    # when Radarr is unreachable and the live snapshot above was skipped).
    movies = [movie for movie in movies if movie["status"] in LIBRARY_STATUSES]
    # A WANTED title Plex already holds is worth a chip here: you are waiting on a
    # download of something your media server can already play. In-library titles get
    # nothing — Radarr holding the file is the stronger, more specific statement.
    server_snapshot = await load_media_server_snapshot(request)
    if server_snapshot is not None:
        for movie in movies:
            if movie["status"] != MovieStatus.WANTED:
                continue
            movie["server_state"] = server_snapshot.holds(
                movie["tmdb_id"], None, movie["title"], movie.get("year")
            )

    # Matched on the normalized title, the same folding of punctuation, diacritics,
    # numerals and articles the weekly search and the pipeline's own matcher use — so
    # "spider man" finds "Spider-Man: Brand New Day" in both search boxes rather than one.
    #
    # A query that normalizes to nothing (an article on its own, e.g. "the") narrows
    # nothing rather than matching nothing: this box filters a library listing, where an
    # unfiltered list is the honest answer, unlike the weekly search, whose whole page is
    # the result set and correctly comes back empty.
    wanted = normalize_title(q)
    if wanted:
        movies = [movie for movie in movies if wanted in movie["normalized_title"]]

    total = len(movies)
    limit = max(PAGE_INCREMENT, limit)
    page = movies[:limit]
    await cache_posters(request, page)

    return render(
        request,
        "dashboard.html",
        active_nav=NAV_KEY,
        movies=page,
        query=q,
        total=total,
        limit=limit,
        has_more=total > limit,
        next_limit=limit + PAGE_INCREMENT,
        page_increment=PAGE_INCREMENT,
        unreachable=[
            apps_by_id[app_id].name for app_id in libraries if app_id not in answered
        ],
        has_any_reports=bool(reports),
        sign_in_notice=sign_in_notice,
    )


@router.get("/posters/{name}")
def poster(request: Request, name: str) -> Response:
    current_user(request)
    path = request.app.state.posters.serve_path(name)
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg")
