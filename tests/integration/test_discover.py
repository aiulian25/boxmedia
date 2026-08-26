"""TV step 11 integration test: the Discover page.

The claim that matters most is negative: **a page render makes no outbound request**.
Every test here that renders the page does so WITHOUT a respx mock, so if a render ever
started reaching the network it would fail on an unmocked call rather than quietly
becoming a page that goes down when Trakt does.
"""

from __future__ import annotations

import httpx
import respx

from app.services.discovery import (
    ANTICIPATED_KEY,
    TRENDING_KEY,
    DiscoverShow,
)
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.sonarr import SonarrSeries
from app.web.discover import DiscoverStatus
from tests.conftest import AppHarness

TRAKT_TRENDING = "https://api.trakt.tv/shows/trending"
TRAKT_ANTICIPATED = "https://api.trakt.tv/shows/anticipated"
TMDB_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TRAKT_ID = "Zx-9QwErTyUiOpAsDfGhJkLzXcVbNm1234567890abc"


def _keys(harness: AppHarness) -> None:
    store = harness.client.app.state.discovery
    store.save("tmdb", TMDB_KEY)
    store.save("trakt", TRAKT_ID)


def _show(
    title: str = "The Hollow Coast",
    *,
    tvdb_id: int | None = 121361,
    tmdb_id: int | None = 1396,
    year: int | None = 2023,
    watchers: int | None = 1842,
    list_count: int | None = None,
) -> DiscoverShow:
    return DiscoverShow(
        tmdb_id=tmdb_id, tvdb_id=tvdb_id, imdb_id="tt0944947", trakt_id=9001,
        title=title, year=year, overview="…", poster_url=None,
        watchers=watchers, list_count=list_count,
    )


def _seed_shelves(harness: AppHarness, trending=None, anticipated=None) -> None:
    harness.client.app.state.discover_cache.save({
        TRENDING_KEY: trending if trending is not None else (_show(),),
        ANTICIPATED_KEY: anticipated if anticipated is not None else (
            _show("Ash Cartography", tvdb_id=424242, tmdb_id=4242, year=2026,
                  watchers=None, list_count=5127),
        ),
    })


def _seed_report(harness: AppHarness) -> None:
    harness.client.app.state.reports.save(Report(
        id="report-20260819-234400-aaaa", week="2026W32",
        run_at="2026-08-19T23:44:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.OK,
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=81_500_000, gross_display="$81.5M", weeks_in_release=4,
            status=MovieStatus.IN_LIBRARY, action=MovieAction.NONE,
            tmdb_id=52001, rating=7.8, genres=["Action", "Thriller"],
        )],
    ))


# --- the page renders from disk, and only from disk ---


def test_the_page_renders_three_rows_from_cache(harness: AppHarness) -> None:
    """No respx mock: a render that reached the network would fail here on an unmocked
    call, which is exactly the guarantee being asserted."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover").text

    assert "This week at the box office" in page
    assert "Trending now" in page
    assert "Anticipated" in page
    assert "Neon Rain" in page
    assert "The Hollow Coast" in page
    assert "Ash Cartography" in page


def test_each_row_names_where_its_ranking_comes_from(harness: AppHarness) -> None:
    """The page's whole honesty rests on this: two rows, two different authorities."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover").text

    assert "Mojo · week 2026W32" in page
    assert "Trakt · artwork from TMDB" in page
    assert "Trakt · not aired yet" in page
    assert "there is no equivalent chart for television" in page


def test_the_week_caption_is_day_first(harness: AppHarness) -> None:
    """Never American — the standing rule for every date this app renders."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)

    page = harness.client.get("/discover").text
    # 2026W32 begins on the 3rd of August. Day first, month second — never American.
    assert "3/8/2026" in page


def test_a_report_less_install_still_renders_the_tv_rows(harness: AppHarness) -> None:
    """The two halves are independent on purpose: a fresh install has no week yet, and
    that must not take television down with it."""
    harness.activate()
    _keys(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover").text

    assert "No week fetched yet" in page
    assert "Mojo · no week fetched yet" in page
    assert "The Hollow Coast" in page


def test_a_failed_run_is_not_treated_as_this_weeks_chart(harness: AppHarness) -> None:
    """`latest()` would hand back a scrape failure. A shelf captioned "this week at the
    box office" must not be built from a run that never got one."""
    harness.activate()
    _keys(harness)
    harness.client.app.state.reports.save(Report(
        id="report-20260820-000000-bbbb", week="2026W33",
        run_at="2026-08-20T00:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED,
        totals=ReportTotals(movies=0, matched=0), movies=[],
    ))

    page = harness.client.get("/discover").text
    assert "No week fetched yet" in page


# --- the chips ---


def test_the_chips_filter_and_are_real_links(harness: AppHarness) -> None:
    """Navigation, not a script filter — so it works with JavaScript off, bookmarks, and
    the back button behaves."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover").text
    assert 'href="/discover?type=movies"' in page
    assert 'href="/discover?type=tv"' in page

    movies_only = harness.client.get("/discover?type=movies").text
    assert "Neon Rain" in movies_only
    assert "Trending now" not in movies_only

    tv_only = harness.client.get("/discover?type=tv").text
    assert "The Hollow Coast" in tv_only
    assert "This week at the box office" not in tv_only


def test_an_unknown_type_shows_the_widest_view(harness: AppHarness) -> None:
    """A bookmarked or hand-edited ?type= should show everything, not an error page."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover?type=chaos").text
    assert "This week at the box office" in page
    assert "Trending now" in page


# --- credentials ---


def test_without_keys_the_page_points_at_settings(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/discover").text

    assert "TMDB API key" in page
    assert "Trakt client ID" in page
    assert 'href="/settings"' in page
    assert "ships none" in page
    # And no Refresh to press, because there is nothing to refresh with.
    assert "/discover/refresh" not in page


def test_refresh_without_a_trakt_id_says_so_rather_than_trying(
    harness: AppHarness,
) -> None:
    """No respx mock: if this reached the network the test would fail on an unmocked
    call, which is the assertion."""
    harness.activate()

    response = harness.client.post("/discover/refresh", follow_redirects=False)
    assert DiscoverStatus.NO_KEYS in response.headers["location"]


# --- state resolution, at render ---


def test_a_series_already_in_sonarr_says_so_and_counts_what_is_missing(
    harness: AppHarness,
) -> None:
    """Resolved at RENDER, not stored: add a series and the card is right immediately
    rather than after the shelf's six-hour TTL."""
    harness.activate()
    _keys(harness)
    _seed_shelves(harness)
    harness.client.app.state.series_cache.save("app-sonarr", (
        SonarrSeries(
            sonarr_id=14, tvdb_id=121361, title="The Hollow Coast", year=2023,
            monitored=True, ended=False, episode_count=34, episode_file_count=26,
        ),
    ))

    page = harness.client.get("/discover").text

    assert "In Sonarr" in page
    assert "Missing 8 episodes" in page


def test_a_complete_series_says_complete(harness: AppHarness) -> None:
    harness.activate()
    _keys(harness)
    _seed_shelves(harness)
    harness.client.app.state.series_cache.save("app-sonarr", (
        SonarrSeries(
            sonarr_id=14, tvdb_id=121361, title="The Hollow Coast", year=2023,
            monitored=True, ended=True, episode_count=34, episode_file_count=34,
        ),
    ))

    assert "Complete — 34 episodes" in harness.client.get("/discover").text


def test_a_show_with_no_tvdb_id_is_honestly_unaddable(harness: AppHarness) -> None:
    """Sonarr keys series on TVDB ids. Offering an Add that cannot work would be worse
    than saying so."""
    harness.activate()
    _keys(harness)
    _seed_shelves(harness, trending=(_show("Riverine", tvdb_id=None),))

    page = harness.client.get("/discover").text
    assert "No TVDB id — search Sonarr by name" in page


def test_a_wanted_series_carries_neither_hint(harness: AppHarness) -> None:
    harness.activate()
    _keys(harness)
    _seed_shelves(harness, trending=(_show(),), anticipated=())

    page = harness.client.get("/discover").text
    assert "Wanted" in page
    assert "Missing" not in page


def test_the_two_counts_are_labelled_for_what_they_measure(harness: AppHarness) -> None:
    """`watchers` is how many are watching now; `list_count` is how many are waiting. A
    bare figure would mean whichever the reader assumed."""
    harness.activate()
    _keys(harness)
    _seed_shelves(harness)

    page = harness.client.get("/discover").text
    assert "1,842 watching" in page
    assert "5,127 waiting" in page


# --- Refresh: the only outbound call ---


@respx.mock
def test_refresh_fills_both_shelves_and_audits(harness: AppHarness) -> None:
    respx.get(TRAKT_TRENDING).mock(return_value=httpx.Response(200, json=[{
        "watchers": 900,
        "show": {"title": "Meridian Fault", "year": 2026,
                 "ids": {"trakt": 1, "tmdb": 5, "tvdb": 7, "imdb": "tt1"}},
    }]))
    respx.get(TRAKT_ANTICIPATED).mock(return_value=httpx.Response(200, json=[{
        "list_count": 4000,
        "show": {"title": "Nightjar", "year": 2026, "ids": {"trakt": 2, "tvdb": 8}},
    }]))
    harness.activate()
    _keys(harness)

    response = harness.client.post("/discover/refresh", follow_redirects=False)
    assert DiscoverStatus.REFRESHED in response.headers["location"]

    rows = harness.client.app.state.discover_cache.load()
    assert [show.title for show in rows[TRENDING_KEY]] == ["Meridian Fault"]
    assert [show.title for show in rows[ANTICIPATED_KEY]] == ["Nightjar"]
    # The four ids a Trakt row carries for free — the reason to rank by Trakt at all.
    assert rows[TRENDING_KEY][0].tvdb_id == 7

    log = (harness.settings.logs_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "discover_refreshed" in log
    assert TRAKT_ID not in log


@respx.mock
def test_a_failed_refresh_keeps_the_previous_shelves(harness: AppHarness) -> None:
    """"We could not look" and "there is nothing" are different claims, and only one of
    them is true."""
    respx.get(TRAKT_TRENDING).mock(side_effect=httpx.ConnectError("gone"))
    respx.get(TRAKT_ANTICIPATED).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)
    _seed_shelves(harness)

    response = harness.client.post("/discover/refresh", follow_redirects=False)
    assert DiscoverStatus.REFRESH_FAILED in response.headers["location"]

    rows = harness.client.app.state.discover_cache.load()
    assert [show.title for show in rows[TRENDING_KEY]] == ["The Hollow Coast"]


def test_refresh_is_a_post_and_carries_csrf(harness: AppHarness) -> None:
    """A refresh spends someone else's rate limit and writes to disk. It is never a GET,
    and never reachable without this session's token."""
    harness.activate()
    _keys(harness)

    page = harness.client.get("/discover").text
    assert 'action="/discover/refresh"' in page
    assert 'name="csrf_token"' in page

    unguarded = harness.client.post(
        "/discover/refresh", data={"csrf_token": "wrong"}, follow_redirects=False
    )
    assert unguarded.status_code == 403


def test_the_page_needs_a_session(harness: AppHarness) -> None:
    response = harness.client.get("/discover", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_the_newest_COMPLETED_run_is_the_chart_even_when_a_newer_one_failed(
    harness: AppHarness,
) -> None:
    """The scenario the earlier test missed. A failed run has no movies, so asserting an
    empty shelf passed whether or not the status was checked. This seeds a failed run
    ON TOP of a good one: the shelf must show the good week's films, not nothing."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    harness.client.app.state.reports.save(Report(
        id="report-20260820-000000-cccc", week="2026W33",
        run_at="2026-08-20T00:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED,
        totals=ReportTotals(movies=0, matched=0), movies=[],
    ))

    page = harness.client.get("/discover").text

    assert "Neon Rain" in page
    assert "Mojo · week 2026W32" in page
    assert "No week fetched yet" not in page


def test_an_unknown_type_highlights_the_all_chip(harness: AppHarness) -> None:
    """`?type=chaos` renders the same SECTIONS as `all`, so content alone cannot tell
    them apart. The chip row can: an untrusted value would leave no chip highlighted,
    and a row of controls where none is active reads as broken."""
    harness.activate()
    _keys(harness)

    import re

    # Whitespace-normalised: the template wraps the href onto its own line, so the two
    # attributes are not contiguous in the raw markup.
    page = re.sub(r"\s+", " ", harness.client.get("/discover?type=chaos").text)

    assert 'class="btn-primary" href="/discover?type=all"' in page
    assert page.count("btn-primary") == 1, "exactly one chip is active, and it is All"


def test_sonarr_wins_over_the_media_server(harness: AppHarness) -> None:
    """The precedence rule, which nothing exercised: a series can be BOTH in Sonarr and
    on Plex. "Already in Sonarr" is the stronger statement — it is the thing that can
    actually fetch the rest — so it must win, and the episode count must be the one that
    tells you what is still missing."""
    from app.services.mediaserver import MediaServerFetch, MediaServerSeries

    harness.activate()
    _keys(harness)
    _seed_shelves(harness, anticipated=())
    harness.client.app.state.series_cache.save("app-sonarr", (
        SonarrSeries(
            sonarr_id=14, tvdb_id=121361, title="The Hollow Coast", year=2023,
            monitored=True, ended=False, episode_count=34, episode_file_count=26,
        ),
    ))
    harness.client.post(
        "/settings/media-server",
        data={"url": "http://plex.local:32400", "token": "t" * 20, "kind": "plex"},
        follow_redirects=False,
    )
    harness.client.app.state.media_server_cache.save(MediaServerFetch(
        movies=(), truncated=False,
        series=(MediaServerSeries(title="The Hollow Coast", year=2023, tvdb_id=121361),),
    ))

    page = harness.client.get("/discover").text

    assert "Missing 8 episodes" in page
    assert "Already on Plex" not in page


@respx.mock
def test_the_tv_tab_does_not_fetch_film_posters(harness: AppHarness) -> None:
    """Invisible in the markup, but not free: computing the film shelf on the TV tab
    would download every film poster to render a section the template then hides.

    Detected with respx and NO route registered. An attempted request raises rather than
    404ing, so it escapes the poster cache's own `except httpx.HTTPError` — which makes
    this the same assertion as the module's headline claim that a render reaches nothing.
    """
    harness.activate()
    _keys(harness)
    harness.client.app.state.reports.save(Report(
        id="report-20260819-234400-dddd", week="2026W32",
        run_at="2026-08-19T23:44:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.OK,
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=81_500_000, gross_display="$81.5M", weeks_in_release=4,
            status=MovieStatus.IN_LIBRARY, action=MovieAction.NONE, tmdb_id=52001,
            poster_url="https://image.tmdb.org/t/p/original/neon.jpg",
        )],
    ))
    _seed_shelves(harness)

    page = harness.client.get("/discover?type=tv").text

    assert "The Hollow Coast" in page
    assert "Neon Rain" not in page
