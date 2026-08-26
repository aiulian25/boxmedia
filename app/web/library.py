"""Library — everything you hold, films and series together (TV step 16).

Was the Box Office dashboard, and the film half is unchanged: only titles actually in
Radarr appear (In Library, or Wanted and awaiting a download), status recomputed against
a live snapshot with the stored status as fallback, posters served locally so the CSP can
stay `img-src \'self\'`. Adding new titles is still a deliberate action on the weekly
report, not here.

## The Movies chip is a regression contract

Ruling 4 of the TV plan: the Movies chip renders exactly what this page rendered before
the merge. `tests/integration/golden_movies_grid.html` is that render, captured against
the old page before a line of this step was written, and a test diffs the two — which is
why the film pipeline below is untouched rather than tidied on the way past.

## Two kinds, one grid, and the ordering that follows

Films arrive in first-sighting order — newest week first, chart rank within it — which is
meaningful and which the Movies chip preserves. Series arrive from a cache with no such
history. So the merged view orders by TITLE, the one key both kinds share honestly, and
each single-kind chip keeps whatever order that kind actually has. Ordering the merged
view by "recency" would mean inventing a date for one half of it.

## What a series card can and cannot say

Everything comes from the snapshot on disk (TV step 8) — no live Sonarr read, on the page
most likely to hold a thousand cards. So the band on a series\' SONARR chip is how much of
it is on disk, not how much is downloading: completeness is in the snapshot and a queue is
not. That also means the chip is deliberately inert to the progress poller, which speaks
in Radarr download percentages and would otherwise repaint a completeness band with one.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi import status as http_status
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.core.sessions import COOKIE_NAME
from app.services.apps import KIND_SONARR, ExternalApp
from app.services.boxoffice import DEFAULT_CURRENCY_SYMBOL, format_gross
from app.services.matcher import normalize_title
from app.services.posters import SERIES_POSTER_WIDTH
from app.services.radarr import RadarrMovie
from app.services.reports import MovieStatus, Report, RunStatus, imdb_url, wiki_url
from app.services.series import CachedSeries
from app.web.deps import (
    MEDIA_TYPES,
    TYPE_MOVIES,
    TYPE_TV,
    cache_posters,
    current_user,
    format_timestamp,
    load_all_radarr_libraries,
    load_all_radarr_queues,
    load_media_server_snapshot,
    parse_timestamp,
    radarr_locations,
    refresh_stale_series_libraries,
    render,
    safe_external_url,
    validated_media_type,
)

router = APIRouter()

NAV_KEY = "library"
LIBRARY_PATH = "/library"
# Kept as a permanent redirect rather than deleted: it is what a bookmark, a reverse-proxy
# rule and every sign-in before this release point at.
DASHBOARD_PATH = "/dashboard"
KIND_MOVIE = "movie"
KIND_SERIES = "series"
# The completeness band is drawn in the same ten-percent steps the download band uses —
# a per-card width would have to be an inline style, and the CSP forbids one.
BAND_STEP = 10
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


def _completeness_step(series: CachedSeries) -> int | None:
    """How much of a series is on disk, in ten-percent steps, or None when unknowable.

    None rather than 0 for a series Sonarr reports no episodes for: an empty band would
    say "you have none of this", and "we do not know how long this is" is a different
    thing. A series with everything reads 100 and the chip fills.
    """
    if series.episode_count <= 0:
        return None
    ratio = series.episode_file_count / series.episode_count
    return min(100, round(ratio * 100 / BAND_STEP) * BAND_STEP)


def _holding_line(series: CachedSeries) -> str:
    """The one line under a series card. A fact either way, in the same words the series
    detail and the Discover shelf already use."""
    if series.complete:
        return f"Complete — {series.episode_count} episodes"
    missing = series.missing_episode_count
    return f"Missing {missing} episode{'s' if missing != 1 else ''}"


def _sonarr_url_for(app: ExternalApp, series: CachedSeries) -> str | None:
    """That series\' page on that Sonarr, or None when it cannot be addressed.

    `radarr_url_for`\'s twin, and the same reasoning: `titleSlug` is what Sonarr\'s own UI
    routes on, the base is the admin-configured address rather than anything
    request-derived, and a record without a slug yields no link instead of a guessed one.
    """
    if not series.title_slug:
        return None
    base = safe_external_url(app.url)
    return f"{base}/series/{quote(series.title_slug, safe='')}" if base else None


def _series_views(request: Request) -> list[dict]:
    """Every series your Sonarr connections hold, from the snapshot on disk.

    No live read: this page can carry a thousand cards, and the snapshot is exactly what
    the Discover shelf and the series detail already judge against — so a card here says
    the same thing they do, without a round trip per render.
    """
    apps_by_id = {app.id: app for app in request.app.state.apps.list_apps(KIND_SONARR)}
    libraries = request.app.state.series_cache.load_all()
    views: list[dict] = []
    seen: set[int] = set()
    for app_id, library in libraries.items():
        app = apps_by_id.get(app_id)
        if app is None:
            continue  # a connection removed since the snapshot was written
        for series in library:
            if series.tvdb_id in seen:
                # Two connections holding the same series is one entry on this page, on
                # the first that answered — the same rule the film half applies to a title
                # sitting on both a 1080p and a 4K box.
                continue
            seen.add(series.tvdb_id)
            views.append({
                "kind": KIND_SERIES,
                "title": series.title,
                "normalized_title": normalize_title(series.title),
                "year": series.year,
                "tmdb_id": series.tmdb_id,
                "tvdb_id": series.tvdb_id,
                "poster_url": series.poster_url,
                "connection": app.name,
                "sonarr_url": _sonarr_url_for(app, series),
                "complete": series.complete,
                "holding": _holding_line(series),
                "band_step": _completeness_step(series),
            })
    return views


def _mark_series_on_server(views: list[dict], snapshot: object) -> None:
    """The media-server hint, for a series Sonarr has not finished.

    Only where it changes what you would do: a complete series is complete, and saying
    "your Plex also has it" adds nothing. An incomplete one is worth knowing about,
    because the episodes you are missing may already be watchable.
    """
    if snapshot is None:
        return
    for view in views:
        if view["complete"]:
            continue
        view["server_state"] = snapshot.holds_series(
            view["tvdb_id"], view["tmdb_id"], None, view["title"], view["year"]
        )


@router.get(DASHBOARD_PATH)
def dashboard(request: Request) -> RedirectResponse:
    """Where this page used to live. 308, not 302: permanent, and it preserves the method
    so a bookmark, a proxy rule and every link written before the merge all still land —
    behind the same session gate, which the middleware applies to the destination."""
    base = request.app.state.settings.url_base
    query = request.url.query
    return RedirectResponse(
        f"{base}{LIBRARY_PATH}{'?' + query if query else ''}",
        status_code=http_status.HTTP_308_PERMANENT_REDIRECT,
    )


@router.get(LIBRARY_PATH)
async def library(
    request: Request, q: str = "", limit: int = DEFAULT_LIMIT, type: str = ""  # noqa: A002
) -> object:
    current_user(request)
    sign_in_notice = _sign_in_notice_view(request)
    media_type = validated_media_type(type)
    reports = request.app.state.reports.list_reports()

    movies = _merge_history(reports)
    # Every connection, not just the primary: a title sent to the 4K box belongs on this
    # page as much as one on the main instance. The queues ride along in the same gather,
    # so live progress costs the slowest single request rather than a second round.
    libraries, queues, _ = await asyncio.gather(
        load_all_radarr_libraries(request),
        load_all_radarr_queues(request),
        # Top up whichever Sonarr snapshots have aged out, in the same gather, so the TV
        # half costs nothing the film half was not already waiting for.
        refresh_stale_series_libraries(request),
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
    for movie in movies:
        movie["kind"] = KIND_MOVIE
    series = _series_views(request)

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
    _mark_series_on_server(series, server_snapshot)

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
        series = [show for show in series if wanted in show["normalized_title"]]

    cards = _chosen(movies, series, media_type)
    total = len(cards)
    limit = max(PAGE_INCREMENT, limit)
    page = cards[:limit]
    # Two passes at two widths — a series poster is a different shape of image and the
    # cache keys on the sized URL. Concurrent within each, as every other page does it.
    await cache_posters(request, [card for card in page if card["kind"] == KIND_MOVIE])
    await cache_posters(
        request,
        [card for card in page if card["kind"] == KIND_SERIES],
        width=SERIES_POSTER_WIDTH,
    )

    return render(
        request,
        "library.html",
        active_nav=NAV_KEY,
        cards=page,
        # Counts of what the SEARCH left, not of the whole library: with a query in the
        # box, the chips answer "how many of each kind matched", which is what a person
        # about to press one of them wants to know.
        counts={
            "all": len(movies) + len(series),
            TYPE_MOVIES: len(movies),
            TYPE_TV: len(series),
        },
        media_type=media_type,
        media_types=MEDIA_TYPES,
        library_path=LIBRARY_PATH,
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
        has_any_series=bool(series),
        sign_in_notice=sign_in_notice,
    )


def _chosen(movies: list[dict], series: list[dict], media_type: str) -> list[dict]:
    """The cards this chip shows, in the order that chip can honestly claim.

    Films carry a meaningful order — newest week first, chart rank within it — and the
    Movies chip keeps it exactly (ruling 4). Nothing merges the two orders honestly, so
    the combined view sorts by the one key both kinds share: the title.
    """
    if media_type == TYPE_MOVIES:
        return movies
    if media_type == TYPE_TV:
        return sorted(series, key=lambda card: card["title"].casefold())
    return sorted(movies + series, key=lambda card: card["title"].casefold())


@router.get("/posters/{name}")
def poster(request: Request, name: str) -> Response:
    current_user(request)
    path = request.app.state.posters.serve_path(name)
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg")
