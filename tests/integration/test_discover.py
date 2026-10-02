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
from app.services.tmdb import TMDB_BASE_URL
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


def _mock_tv_detail(tmdb_id: int, *, poster_path: str | None) -> None:
    """What TMDB answers for one show. Trakt returns ids and titles and never artwork,
    so this is where a Discover card's picture comes from."""
    respx.get(f"{TMDB_BASE_URL}/tv/{tmdb_id}").mock(return_value=httpx.Response(200, json={
        "id": tmdb_id, "name": "Meridian Fault", "poster_path": poster_path,
        "external_ids": {"tvdb_id": 7},
    }))


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
    # 2026W32 begins Friday the 7th of August — Mojo's own "Aug 7-13". Day first, month
    # second — never American.
    assert "7/8/2026" in page


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


def test_a_series_you_do_not_have_says_missing_not_wanted(harness: AppHarness) -> None:
    """The bottom rung, and the word it must not use.

    Sonarr's "wanted" means a series you ALREADY added, waiting on a release. On this
    shelf the same badge meant the opposite — nothing you own has it — which reads as a
    promise the app never made. `missing` is the word this app already uses for that.
    """
    harness.activate()
    _keys(harness)
    _seed_shelves(harness, trending=(_show(),), anticipated=())

    page = harness.client.get("/discover").text

    assert ">Missing<" in page
    assert ">Wanted<" not in page
    # And nothing that would claim you hold it.
    assert "In Sonarr" not in page
    assert "on Plex" not in page


def test_the_film_shelf_keeps_radarrs_own_word(harness: AppHarness) -> None:
    """Only television changed. On the film shelf "Wanted" is Radarr's meaning — the
    title is in your library, awaiting a release — so renaming it there would break the
    vocabulary the Library page and the movie modal both use."""
    harness.activate()
    _keys(harness)
    _seed_report(harness)
    _seed_shelves(harness, trending=(), anticipated=())

    page = harness.client.get("/discover?type=movies").text

    assert ">Wanted<" in page or ">In Library<" in page
    assert ">Missing<" not in page


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
    _mock_tv_detail(5, poster_path="/meridian.jpg")
    harness.activate()
    _keys(harness)

    response = harness.client.post("/discover/refresh", follow_redirects=False)
    assert DiscoverStatus.REFRESHED in response.headers["location"]

    rows = harness.client.app.state.discover_cache.load()
    assert [show.title for show in rows[TRENDING_KEY]] == ["Meridian Fault"]
    assert [show.title for show in rows[ANTICIPATED_KEY]] == ["Nightjar"]
    # The four ids a Trakt row carries for free — the reason to rank by Trakt at all.
    assert rows[TRENDING_KEY][0].tvdb_id == 7
    # And the half Trakt cannot answer, filled in on the way past.
    assert "meridian.jpg" in rows[TRENDING_KEY][0].poster_url

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


# --- the artwork Trakt cannot answer (found by the step 19 bring-up) ---


@respx.mock
def test_a_show_tmdb_has_never_heard_of_costs_only_its_own_picture(
    harness: AppHarness,
) -> None:
    """Every poster lookup is its own. One 404 must not empty a shelf of twenty."""
    respx.get(TRAKT_TRENDING).mock(return_value=httpx.Response(200, json=[
        {"watchers": 900, "show": {"title": "Meridian Fault", "year": 2026,
                                   "ids": {"trakt": 1, "tmdb": 5, "tvdb": 7}}},
        {"watchers": 800, "show": {"title": "Nightjar", "year": 2026,
                                   "ids": {"trakt": 2, "tmdb": 6, "tvdb": 8}}},
    ]))
    respx.get(TRAKT_ANTICIPATED).mock(return_value=httpx.Response(200, json=[]))
    _mock_tv_detail(5, poster_path="/meridian.jpg")
    respx.get(f"{TMDB_BASE_URL}/tv/6").mock(return_value=httpx.Response(404, json={}))
    harness.activate()
    _keys(harness)

    harness.client.post("/discover/refresh", follow_redirects=False)

    rows = harness.client.app.state.discover_cache.load()[TRENDING_KEY]
    assert len(rows) == 2
    assert "meridian.jpg" in rows[0].poster_url
    assert rows[1].poster_url is None  # no picture, still on the shelf


@respx.mock
def test_a_shelf_without_a_tmdb_key_is_titles_and_states(harness: AppHarness) -> None:
    """The two credentials do different jobs: Trakt ranks, TMDB illustrates. With only
    the first, a refresh still works and the cards still say what you hold."""
    respx.get(TRAKT_TRENDING).mock(return_value=httpx.Response(200, json=[
        {"watchers": 900, "show": {"title": "Meridian Fault", "year": 2026,
                                   "ids": {"trakt": 1, "tmdb": 5, "tvdb": 7}}},
    ]))
    respx.get(TRAKT_ANTICIPATED).mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    harness.client.app.state.discovery.save("trakt", TRAKT_ID)  # Trakt only

    response = harness.client.post("/discover/refresh", follow_redirects=False)

    assert DiscoverStatus.REFRESHED in response.headers["location"]
    rows = harness.client.app.state.discover_cache.load()[TRENDING_KEY]
    assert [show.title for show in rows] == ["Meridian Fault"]
    assert rows[0].poster_url is None


@respx.mock
def test_one_lookup_per_show_even_when_both_shelves_carry_it(
    harness: AppHarness,
) -> None:
    """The shelves overlap — a show can be trending AND anticipated — and forty cards
    must not become forty-plus requests on one button press."""
    row = {"watchers": 900, "show": {"title": "Meridian Fault", "year": 2026,
                                     "ids": {"trakt": 1, "tmdb": 5, "tvdb": 7}}}
    respx.get(TRAKT_TRENDING).mock(return_value=httpx.Response(200, json=[row]))
    respx.get(TRAKT_ANTICIPATED).mock(return_value=httpx.Response(200, json=[
        {"list_count": 10, "show": row["show"]},
    ]))
    detail = respx.get(f"{TMDB_BASE_URL}/tv/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "name": "Meridian Fault",
                                               "poster_path": "/meridian.jpg"})
    )
    harness.activate()
    _keys(harness)

    harness.client.post("/discover/refresh", follow_redirects=False)

    assert detail.call_count == 1
    cache = harness.client.app.state.discover_cache.load()
    assert "meridian.jpg" in cache[TRENDING_KEY][0].poster_url
    assert "meridian.jpg" in cache[ANTICIPATED_KEY][0].poster_url


def test_a_refresh_with_unreadable_keys_says_which_problem_it_is(
    harness: AppHarness,
) -> None:
    """Stored keys this install's encryption key cannot open are their own answer.

    Not the refresh-failed banner beside it: that one says Trakt could not be reached,
    and with an unreadable key nothing is ever asked of Trakt. No respx mock, so a
    regression that started making the call would fail on an unmocked request.
    """
    from app.core import crypto
    from app.services.discovery import DiscoveryStore

    harness.activate()
    _keys(harness)
    harness.client.app.state.discovery = DiscoveryStore(
        harness.settings.config_dir, key=crypto.generate_key()
    )

    response = harness.client.post("/discover/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert DiscoverStatus.KEYS_UNREADABLE in response.headers["location"]
    assert harness.client.get("/discover").status_code == 200


def test_refresh_builds_both_public_clients_without_the_tls_escape_hatch(
    tmp_path, monkeypatch
) -> None:
    """Refresh was already correct; this pins it so a later "consistency" edit cannot
    hand TMDB and Trakt the CA file meant for the user's own servers."""
    from tests.conftest import build_harness

    built: list[dict] = []

    class _Recorder:
        def __init__(self, credential, **kwargs):
            built.append(kwargs)
            self.credential = credential

        async def trending_shows(self, limit):
            return []

        async def anticipated_shows(self, limit):
            return []

    ca_file = tmp_path / "home-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nnot a real CA\n-----END CERTIFICATE-----\n")
    harness = build_harness(tmp_path, outbound_tls_verify=False, tls_ca_file=ca_file)
    harness.activate()
    _keys(harness)
    monkeypatch.setattr("app.web.discover.TraktClient", _Recorder)
    monkeypatch.setattr("app.web.discover.TmdbClient", _Recorder)

    harness.client.post("/discover/refresh", follow_redirects=False)

    assert built, "refresh built no client at all"
    for kwargs in built:
        assert "verify" not in kwargs, kwargs


# --- Refresh returns you to the chip you pressed it from ---


@respx.mock
def test_refreshing_from_the_tv_chip_lands_back_on_tv(harness: AppHarness) -> None:
    """Pressing Refresh while reading TV used to drop the reader on All, which is the
    widest shelf and not the one they were looking at."""
    respx.get(TRAKT_TRENDING).mock(side_effect=httpx.ConnectError("gone"))
    respx.get(TRAKT_ANTICIPATED).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)

    response = harness.client.post(
        "/discover/refresh", data={"type": "tv"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "type=tv" in response.headers["location"]
    # The banner still travels with it.
    assert DiscoverStatus.REFRESH_FAILED in response.headers["location"]


@respx.mock
def test_refreshing_from_movies_lands_back_on_movies(harness: AppHarness) -> None:
    respx.get(TRAKT_TRENDING).mock(side_effect=httpx.ConnectError("gone"))
    respx.get(TRAKT_ANTICIPATED).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)

    response = harness.client.post(
        "/discover/refresh", data={"type": "movies"}, follow_redirects=False
    )

    assert "type=movies" in response.headers["location"]


@respx.mock
def test_a_refresh_that_names_no_chip_still_works(harness: AppHarness) -> None:
    """An older cached page, or a client that posts nothing, must not 422."""
    respx.get(TRAKT_TRENDING).mock(side_effect=httpx.ConnectError("gone"))
    respx.get(TRAKT_ANTICIPATED).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)

    response = harness.client.post("/discover/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert "type=all" in response.headers["location"]


@respx.mock
def test_a_crafted_chip_never_reaches_the_location_header(harness: AppHarness) -> None:
    """The value becomes a URL, so it is closed to the three the chip row renders before
    it is interpolated — the same rule the theme setting follows."""
    respx.get(TRAKT_TRENDING).mock(side_effect=httpx.ConnectError("gone"))
    respx.get(TRAKT_ANTICIPATED).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)

    response = harness.client.post(
        "/discover/refresh",
        data={"type": "https://evil.test/steal?x="},
        follow_redirects=False,
    )

    location = response.headers["location"]
    assert "evil.test" not in location
    assert location.endswith("type=all")


def test_the_refresh_form_carries_the_open_chip(harness: AppHarness) -> None:
    """Without the hidden field the server cannot know which shelf to come back to."""
    harness.activate()
    _keys(harness)

    page = harness.client.get("/discover?type=tv").text

    assert '<input type="hidden" name="type" value="tv">' in page
