"""Finding a title across every stored week, instead of opening each report in turn."""

from __future__ import annotations

import httpx
import respx

from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.web.reports import MAX_QUERY_LENGTH, MAX_SEARCH_RESULTS, SEARCH_PATH
from tests.conftest import AppHarness

RADARR_URL = "http://radarr.local:7878"
API = f"{RADARR_URL}/api/v3"
KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — the suite's dummy key
SPIDER_TMDB = 5001


def _movie(rank: int, title: str, tmdb: int, *, year: int = 2026) -> MovieResult:
    return MovieResult(
        rank=rank, title=title, normalized_title=title.lower().replace("-", " "),
        gross_amount=rank * 1_000_000, gross_display=f"${rank}.0M", weeks_in_release=1,
        status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=tmdb, year=year,
    )


def _week(harness: AppHarness, week: str, movies: list[MovieResult]) -> str:
    report_id = f"report-{week}-100000-rpt"
    harness.client.app.state.reports.save(Report(
        id=report_id, run_at=f"2026-08-12T10:00:{int(week[-2:]) % 60:02d}+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
        totals=ReportTotals(movies=len(movies), matched=0), movies=movies,
    ))
    return report_id


def _seed_three_weeks(harness: AppHarness) -> None:
    _week(harness, "2026W29", [_movie(4, "Spider-Man: Brand New Day", SPIDER_TMDB)])
    _week(harness, "2026W30", [_movie(2, "Spider-Man: Brand New Day", SPIDER_TMDB),
                               _movie(3, "The Odyssey", 5002)])
    _week(harness, "2026W31", [_movie(1, "Spider-Man: Brand New Day", SPIDER_TMDB)])


def test_a_title_is_found_with_every_week_it_charted(harness: AppHarness) -> None:
    """The whole point: one search instead of opening each week's report in turn."""
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    assert "Spider-Man: Brand New Day" in page
    assert "W31 · #1" in page and "W30 · #2" in page and "W29 · #4" in page
    assert "The Odyssey" not in page  # not a match


def test_each_week_links_to_that_report(harness: AppHarness) -> None:
    """Jumping straight to the week is the point — otherwise the search only tells you
    a report exists somewhere."""
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    for week in ("2026W29", "2026W30", "2026W31"):
        assert f'href="/reports/report-{week}-100000-rpt#movie-' in page


def test_weeks_are_listed_newest_first(harness: AppHarness) -> None:
    """Ordered by WEEK, not by when the report was written: re-running an old week makes
    its report the newest, and its chip must not jump to the front of the run."""
    harness.activate()
    _seed_three_weeks(harness)
    # A re-run of the OLDEST week, recorded now — the newest report of the oldest week.
    harness.client.app.state.reports.save(Report(
        id="report-2026W29-999999-rerun", run_at="2026-08-31T23:59:59+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W29",
        totals=ReportTotals(movies=1, matched=0),
        movies=[_movie(4, "Spider-Man: Brand New Day", SPIDER_TMDB)],
    ))

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text
    chips = [
        chunk.partition(">")[2].split("</a>")[0].strip()
        for chunk in page.split('class="week-chip" href')[1:]
    ]

    assert chips == ["W31 · #1", "W30 · #2", "W29 · #4"]


def test_matching_folds_punctuation_like_the_pipeline_does(harness: AppHarness) -> None:
    """"spider man" finds "Spider-Man" — the same normalisation that lets the pipeline
    recognise one film across two spellings."""
    harness.activate()
    _seed_three_weeks(harness)

    assert "Spider-Man" in harness.client.get(SEARCH_PATH, params={"q": "spider man"}).text


def test_a_title_charted_twice_appears_once(harness: AppHarness) -> None:
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    assert page.count('class="search-title"') == 1


def test_an_empty_query_asks_rather_than_listing_everything(harness: AppHarness) -> None:
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "  "}).text

    assert "Type part of a title" in page
    assert "Spider-Man" not in page


def test_no_match_says_only_fetched_weeks_are_searchable(harness: AppHarness) -> None:
    """A user who cannot find a film needs to know the difference between "not in your
    history" and "not a film" — otherwise they go hunting through the weeks again."""
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "nosuchfilm"}).text

    assert "Nothing matching" in page
    assert "Only weeks you have fetched can be searched" in page


def test_an_over_long_query_is_clipped(harness: AppHarness) -> None:
    # It is echoed into the heading; an unbounded value would stretch the dialog.
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "x" * 500}).text

    assert "x" * (MAX_QUERY_LENGTH + 1) not in page


def test_the_query_is_escaped_not_rendered(harness: AppHarness) -> None:
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "<img src=x onerror=alert(1)>"}).text

    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_results_are_capped(harness: AppHarness) -> None:
    # A one-letter query legitimately matches most of the history.
    harness.activate()
    for index in range(MAX_SEARCH_RESULTS + 10):
        _week(harness, f"2026W{index:02d}", [_movie(1, f"Film Alpha {index}", 6000 + index)])

    page = harness.client.get(SEARCH_PATH, params={"q": "alpha"}).text

    assert page.count('class="search-title"') == MAX_SEARCH_RESULTS


def test_the_fragment_is_the_modal_body_not_a_whole_page(harness: AppHarness) -> None:
    """app.js drops this straight into the dialog, so it must not carry a page shell."""
    harness.activate()
    _seed_three_weeks(harness)

    fragment = harness.client.get(SEARCH_PATH, params={"q": "spider", "fragment": "1"}).text

    assert "<!DOCTYPE html>" not in fragment
    assert "<nav" not in fragment
    assert "Spider-Man: Brand New Day" in fragment


def test_without_javascript_the_same_url_is_a_full_page(harness: AppHarness) -> None:
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    assert "<!DOCTYPE html>" in page
    assert "Back to reports" in page


@respx.mock
def test_a_title_already_in_radarr_says_so_instead_of_offering_add(
    harness: AppHarness,
) -> None:
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=KEY)
    _seed_three_weeks(harness)
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(
        200, json=[{"tmdbId": SPIDER_TMDB, "id": 9, "hasFile": True, "title": "Spider-Man"}]))

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    assert "In Library" in page and "Main" in page
    assert "Add to Radarr" not in page


@respx.mock
def test_a_title_not_in_radarr_offers_the_add_control(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=KEY)
    _seed_three_weeks(harness)
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    assert "Add to Radarr" in page
    assert 'action="/add-movie"' in page
    assert 'name="csrf_token"' in page
    assert f'name="tmdb_id" value="{SPIDER_TMDB}"' in page


def test_the_reports_page_carries_the_search_form(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/reports").text

    assert f'action="{SEARCH_PATH}"' in page
    assert "data-modal" in page  # app.js upgrades it into the dialog
    assert 'method="get"' in page  # shareable, and carries no CSRF token


def test_search_is_not_swallowed_by_the_report_detail_route(harness: AppHarness) -> None:
    """/reports/{report_id} is a wildcard; registered first it reads "search" as an id
    and redirects to the list instead of searching."""
    harness.activate()
    _seed_three_weeks(harness)

    response = harness.client.get(SEARCH_PATH, params={"q": "spider"}, follow_redirects=False)

    assert response.status_code == 200


# --- landing on the title itself, not the top of a chart of ten ---


def _hrefs(page: str) -> list[str]:
    return [
        chunk.split('"')[1]
        for chunk in page.split('class="week-chip" href=')[1:]
    ]


def test_a_week_link_lands_on_the_title_that_was_searched_for(
    harness: AppHarness,
) -> None:
    """The whole point of the jump: a chart of ten is a lot to re-read when you already
    said which film you wanted. The fragment names the film's rank in THAT week."""
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    # Spider-Man is #1 in W31, #2 in W30, #4 in W29 — each chip carries its own week's
    # rank, not one rank reused across all three.
    assert _hrefs(page) == [
        "/reports/report-2026W31-100000-rpt#movie-1",
        "/reports/report-2026W30-100000-rpt#movie-2",
        "/reports/report-2026W29-100000-rpt#movie-4",
    ]


def test_following_a_week_link_finds_the_anchor_on_the_report(
    harness: AppHarness,
) -> None:
    """A round trip, because a fragment nothing answers is a link that silently does
    nothing: follow every chip and check the report really carries that id."""
    harness.activate()
    _seed_three_weeks(harness)

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text

    for href in _hrefs(page):
        path, _, fragment = href.partition("#")
        report = harness.client.get(path).text
        assert f'id="{fragment}"' in report, f"{path} has no {fragment}"


def test_the_anchor_belongs_to_the_right_film(harness: AppHarness) -> None:
    """W30 holds two films; landing on #movie-2 must reach Spider-Man, not The Odyssey
    at #movie-3."""
    harness.activate()
    _seed_three_weeks(harness)

    report = harness.client.get("/reports/report-2026W30-100000-rpt").text
    card = report.split('id="movie-2"')[1].split('id="movie-3"')[0]

    assert "Spider-Man: Brand New Day" in card
    assert "The Odyssey" not in card


def test_every_card_on_a_report_can_be_linked_to(harness: AppHarness) -> None:
    harness.activate()
    _seed_three_weeks(harness)

    report = harness.client.get("/reports/report-2026W30-100000-rpt").text

    assert report.count('class="poster-card') == 2
    for rank in (2, 3):
        assert f'id="movie-{rank}"' in report


# --- chips name their year once a title's weeks span more than one ---


def test_chips_name_the_year_when_a_title_charted_across_two(harness: AppHarness) -> None:
    """Two Januaries make "W02 · #4" name two different weeks, and the chips beside it
    stop being distinguishable."""
    harness.activate()
    _week(harness, "2025W02", [_movie(4, "Spider-Man: Brand New Day", SPIDER_TMDB)])
    _week(harness, "2026W02", [_movie(1, "Spider-Man: Brand New Day", SPIDER_TMDB)])

    page = harness.client.get(SEARCH_PATH, params={"q": "spider"}).text
    chips = _chip_labels(page)

    assert chips == ["W02 ’26 · #1", "W02 ’25 · #4"]


def test_chips_stay_compact_within_one_year(harness: AppHarness) -> None:
    """The regression pin: a single-year history reads exactly as it did before."""
    harness.activate()
    _seed_three_weeks(harness)

    chips = _chip_labels(harness.client.get(SEARCH_PATH, params={"q": "spider"}).text)

    assert chips == ["W31 · #1", "W30 · #2", "W29 · #4"]
    assert not any("’" in chip for chip in chips)


def test_each_title_is_judged_on_its_own_weeks(harness: AppHarness) -> None:
    """Decided per list, not per page: a title that charted in one year reads better
    without the year even while another title on the same page carries it."""
    harness.activate()
    _week(harness, "2025W02", [_movie(4, "Spider-Man: Brand New Day", SPIDER_TMDB)])
    _week(harness, "2026W02", [_movie(1, "Spider-Man: Brand New Day", SPIDER_TMDB),
                               _movie(2, "The Odyssey", 5002)])

    page = harness.client.get(SEARCH_PATH, params={"q": "e"}).text
    spider = page.split("Spider-Man")[1]
    odyssey = page.split("The Odyssey")[1]

    assert "’25" in spider or "’26" in spider   # two years -> labelled
    assert "’" not in odyssey.split("</div>")[0]  # one year -> compact


def _chip_labels(page: str) -> list[str]:
    return [
        chunk.partition(">")[2].split("</a>")[0].strip()
        for chunk in page.split('class="week-chip" href')[1:]
    ]
