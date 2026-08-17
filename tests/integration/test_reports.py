"""Step 17 test: reports list, detail, delete + audit; failed-run rendering."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from app.services.boxoffice import BoxOfficeEntry, bom_week_id
from app.services.filters import SCHEDULE_MODE_CADENCE, SCHEDULE_MODE_INTERVAL
from app.services.radarr import RadarrLookupResult, RadarrMovie
from app.services.reports import (
    MATCHED_BY_GUESS,
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.scheduler import BoxMediaScheduler
from app.web import reports as report_routes
from app.web.deps import format_timestamp
from app.web.reports import NEVER_RAN
from tests.conftest import AppHarness

STEP12_URL = "http://step12.radarr:7878"
STEP12_API = f"{STEP12_URL}/api/v3"
STEP12_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105

RADARR_URL = "http://127.0.0.1:1"
RADARR_KEY = "0123456789abcdef0123456789abcdef"


def _save(harness: AppHarness, report: Report) -> None:
    harness.client.app.state.reports.save(report)


def _ok_report(report_id: str) -> Report:
    movie = MovieResult(
        rank=1, title="Neon Rain", normalized_title="neon rain", gross_amount=45_000_000,
        gross_display="$45.0M", weeks_in_release=1, status=MovieStatus.WANTED,
        action=MovieAction.ADDED, tmdb_id=555,
    )
    return Report(
        id=report_id, run_at="2026-08-12T11:08:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W27", totals=ReportTotals(movies=1, matched=1),
        movies=[movie],
    )


def test_week_start_display() -> None:
    from app.web.reports import _week_start_display

    assert _week_start_display("2026W27") == "29/6/2026"  # Monday of ISO week 27, day-first
    assert _week_start_display("2026W01") == "29/12/2025"  # ISO week 1 spills into prev year
    assert _week_start_display("current") is None
    assert _week_start_display("garbage") is None


def test_reports_list_shows_cards(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ok_report("report-20260812-110800-aaaa"))
    page = harness.client.get("/reports")
    assert page.status_code == 200
    assert "2026W27" in page.text
    assert "29/6/2026" in page.text  # the week's start date, day-first (not the fetch time)
    assert "Matched" in page.text


def test_report_detail_lists_movies(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ok_report("report-20260812-110800-bbbb"))
    page = harness.client.get("/reports/report-20260812-110800-bbbb")
    assert "Neon Rain" in page.text
    assert "poster-grid" in page.text  # visual grid, not a text table
    # With Radarr unreachable in tests, a stored-wanted title shows as Wanted.
    assert "Wanted" in page.text


def test_failed_report_shows_error(harness: AppHarness) -> None:
    harness.activate()
    _save(
        harness,
        Report(
            id="report-20260812-120000-cccc", run_at="2026-08-12T12:00:00+00:00",
            trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED,
            totals=ReportTotals(movies=0, matched=0), error="layout changed",
        ),
    )
    detail = harness.client.get("/reports/report-20260812-120000-cccc")
    assert "layout changed" in detail.text


def test_delete_removes_one_and_audits(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ok_report("report-20260812-110800-dddd"))
    _save(harness, _ok_report("report-20260812-090000-eeee"))
    response = harness.client.post(
        "/reports/report-20260812-110800-dddd/delete", follow_redirects=False
    )
    assert response.status_code == 303
    remaining = [r.id for r in harness.client.app.state.reports.list_reports()]
    assert remaining == ["report-20260812-090000-eeee"]
    assert "report_deleted" in "\n".join(harness.audit_lines())


def test_rerun_creates_fresh_report_for_same_week(harness: AppHarness) -> None:
    harness.activate()
    # No Radarr configured, so the run records a no_app report — enough to prove
    # a re-run of a given week produces a new report and reaches the pipeline.
    before = len(harness.client.app.state.reports.list_reports())
    response = harness.client.post("/run", data={"week": "2026W02"}, follow_redirects=False)
    assert response.status_code == 303
    assert "/reports/report-" in response.headers["location"]  # lands on the new report
    reports = harness.client.app.state.reports.list_reports()
    assert len(reports) == before + 1
    assert reports[0].week == "2026W02"


def test_fetch_specific_week_by_date(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/run", data={"week_date": "2026-01-09"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert harness.client.app.state.reports.latest().week == "2026W02"


def test_run_with_bad_date_reports_error_not_current_week(harness: AppHarness) -> None:
    # A malformed date must surface an error, not silently fetch the current week.
    harness.activate()
    before = len(harness.client.app.state.reports.list_reports())
    response = harness.client.post(
        "/run", data={"week_date": "not-a-date"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/reports?status=bad_week")
    assert len(harness.client.app.state.reports.list_reports()) == before  # nothing ran
    page = harness.client.get("/reports?status=bad_week")
    assert "pick a week with the date field" in page.text


def test_unknown_report_redirects(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.get("/reports/report-does-not-exist", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/reports")


@respx.mock
def test_report_detail_caches_posters(harness: AppHarness) -> None:
    # cache_posters (now concurrent) must still fetch + cache a poster and set the
    # same-origin poster_local so the grid renders a local <img>. No Radarr app is
    # configured, so only the poster URL is fetched.
    harness.activate()
    poster_url = "http://img.local/MediaCover/1/poster.jpg"
    respx.get(poster_url).mock(return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg"))
    movie = MovieResult(
        rank=1, title="Neon Rain", normalized_title="neon rain", gross_amount=45_000_000,
        gross_display="$45.0M", weeks_in_release=1, status=MovieStatus.WANTED,
        action=MovieAction.NONE, tmdb_id=555, poster_url=poster_url,
    )
    _save(
        harness,
        Report(
            id="report-20260812-110800-pppp", run_at="2026-08-12T11:08:00+00:00",
            trigger=RunTrigger.MANUAL, status=RunStatus.OK,
            totals=ReportTotals(movies=1, matched=1), movies=[movie],
        ),
    )
    page = harness.client.get("/reports/report-20260812-110800-pppp")
    assert "/posters/" in page.text  # poster_local was set from the local cache


class _NonAddingRadarr:
    """Records any add attempt so the test can prove a run never makes one."""

    def __init__(self) -> None:
        self.added: list[dict] = []

    async def list_movies(self) -> list[RadarrMovie]:
        return []  # empty library -> every chart title is "missing"

    async def lookup(self, term: str) -> list[RadarrLookupResult]:
        return [
            RadarrLookupResult(
                tmdb_id=555, title=term, year=2025, overview="x",
                poster_url=None, genres=("Action",), imdb_id="tt555", rating=7.0,
            )
        ]

    async def add_movie(self, **kwargs: object) -> RadarrMovie:
        self.added.append(kwargs)
        return RadarrMovie(tmdb_id=555, title="x", year=2025, has_file=False)


def test_run_route_never_adds_to_radarr(harness: AppHarness) -> None:
    """The reported scenario: Radarr connected, click Run Current Week. The run
    must produce a report and add NOTHING — adding is a manual, per-title action."""
    harness.activate()
    harness.client.post(
        "/settings/apps",
        data={"name": "Radarr", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )

    fake = _NonAddingRadarr()
    pipeline = harness.client.app.state.pipeline
    pipeline._make_radarr = lambda _app_id: fake  # noqa: SLF001 — patch wired instance

    async def _chart(_week: str | None = None) -> tuple[str, list[BoxOfficeEntry]]:
        entry = BoxOfficeEntry(
            rank=1, title="Neon Rain", gross_amount=1_000_000, weeks_in_release=1
        )
        return ("current", [entry])

    pipeline._fetch_chart = _chart  # noqa: SLF001

    response = harness.client.post("/run", follow_redirects=False)
    assert response.status_code == 303

    report = harness.client.app.state.reports.latest()
    assert report.status == RunStatus.OK
    assert report.movies[0].status == MovieStatus.MISSING
    assert report.movies[0].action == MovieAction.NONE
    assert fake.added == []  # the run added nothing to Radarr


# --- manual match fixer (F10) ---

FIX_RADARR_URL = "http://radarr.local:7878"
FIX_API = f"{FIX_RADARR_URL}/api/v3"
LOOKUP_RESULTS = [
    {"tmdbId": 111, "title": "Cookie Queens", "year": 2026, "overview": "The right one.",
     "images": [{"coverType": "poster", "remoteUrl": "http://img/ok.jpg"}], "imdbId": "tt111"},
    {"tmdbId": 222, "title": "Cookie Queens: Behind the Scenes", "year": 2026,
     "overview": "A documentary.", "images": [], "imdbId": "tt222"},
]


def _no_match_report(harness: AppHarness, report_id: str) -> None:
    movie = MovieResult(
        rank=1, title="Cookie Queens", normalized_title="cookie queens",
        gross_amount=486_618, gross_display="$0.5M", weeks_in_release=1,
        status=MovieStatus.MISSING, action=MovieAction.NO_MATCH,  # no tmdb_id: a dead end
    )
    _save(harness, Report(
        id=report_id, run_at="2026-08-14T11:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W32", totals=ReportTotals(movies=1, matched=0),
        movies=[movie],
    ))
    harness.client.app.state.apps.add(name="Radarr", url=FIX_RADARR_URL, api_key=RADARR_KEY)


@respx.mock
def test_no_match_title_can_be_fixed_and_then_added(harness: AppHarness) -> None:
    report_id = "report-20260814-110000-fixm"
    respx.get(f"{FIX_API}/movie/lookup").mock(
        return_value=httpx.Response(200, json=LOOKUP_RESULTS)
    )
    respx.get(f"{FIX_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    # The detail page also loads Radarr's profiles/folders for the add form.
    respx.get(f"{FIX_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{FIX_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    # Once fixed, the card has a poster the detail page caches locally.
    respx.get("http://img/ok.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg")
    )
    harness.activate()
    _no_match_report(harness, report_id)

    # Before: the card is a dead end — no add form for it.
    assert "Add to Radarr as" not in harness.client.get(f"/reports/{report_id}").text

    # 1. Search Radarr from the card.
    page = harness.client.get(
        f"/reports/{report_id}/fix-match", params={"rank": 1, "term": "Cookie Queens"}
    )
    assert page.status_code == 200
    assert "Cookie Queens: Behind the Scenes" in page.text  # both candidates offered
    assert "Use this" in page.text

    # 2. Pick the right film.
    response = harness.client.post(
        "/fix-match",
        data={"report_id": report_id, "rank": "1", "term": "Cookie Queens", "tmdb_id": "111"},
        follow_redirects=False,
    )
    assert "status=match_fixed" in response.headers["location"]

    stored = harness.client.app.state.reports.get(report_id).movies[0]
    assert stored.tmdb_id == 111
    assert stored.year == 2026
    assert stored.imdb_url == "https://www.imdb.com/title/tt111/"
    assert stored.action == MovieAction.NONE
    assert "match_corrected" in "\n".join(harness.audit_lines())

    # 3. The card now offers the normal add form.
    assert "Add to Radarr" in harness.client.get(f"/reports/{report_id}").text


@respx.mock
def test_fix_match_ignores_a_tmdb_id_radarr_did_not_offer(harness: AppHarness) -> None:
    # Only Radarr's own results may be stored — a forged tmdb_id is refused, so nothing
    # the browser sends can put unvetted metadata into a report.
    report_id = "report-20260814-110000-forg"
    respx.get(f"{FIX_API}/movie/lookup").mock(
        return_value=httpx.Response(200, json=LOOKUP_RESULTS)
    )
    harness.activate()
    _no_match_report(harness, report_id)

    response = harness.client.post(
        "/fix-match",
        data={"report_id": report_id, "rank": "1", "term": "Cookie Queens", "tmdb_id": "999"},
        follow_redirects=False,
    )
    assert "status=add_failed" in response.headers["location"]
    assert harness.client.app.state.reports.get(report_id).movies[0].tmdb_id is None


@respx.mock
def test_fix_match_survives_an_unreachable_radarr(harness: AppHarness) -> None:
    report_id = "report-20260814-110000-down"
    respx.get(f"{FIX_API}/movie/lookup").mock(side_effect=httpx.ConnectError("refused"))
    harness.activate()
    _no_match_report(harness, report_id)

    page = harness.client.get(
        f"/reports/{report_id}/fix-match", params={"rank": 1, "term": "x"},
        follow_redirects=False,
    )
    assert page.status_code == 303
    assert "status=add_failed" in page.headers["location"]


# --- current quality profile in the upgrade flow (F12) ---


@respx.mock
def test_upgrade_summary_shows_and_preselects_the_current_profile(harness: AppHarness) -> None:
    report_id = "report-20260814-120000-prof"
    # The movie is in Radarr, downloaded, on profile 5.
    respx.get(f"{FIX_API}/movie").mock(return_value=httpx.Response(200, json=[{
        "tmdbId": 777, "id": 12, "title": "Neon Rain", "year": 2025, "hasFile": True,
        "qualityProfileId": 5,
        "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}},
    }]))
    respx.get(f"{FIX_API}/qualityprofile").mock(return_value=httpx.Response(200, json=[
        {"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"},
    ]))
    respx.get(f"{FIX_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    harness.activate()
    harness.client.app.state.apps.add(name="Radarr", url=FIX_RADARR_URL, api_key=RADARR_KEY)
    _save(harness, Report(
        id=report_id, run_at="2026-08-14T12:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain", gross_amount=1,
            gross_display="$0.0M", weeks_in_release=1, status=MovieStatus.IN_LIBRARY,
            action=MovieAction.NONE, tmdb_id=777,
        )],
    ))

    page = harness.client.get(f"/reports/{report_id}").text
    assert "profile: Ultra-HD" in page  # the summary names what it has now
    assert '<option value="5" selected>Ultra-HD · current</option>' in page
    assert '<option value="4" >HD-1080p</option>' in page  # the default is NOT preselected


@respx.mock
def test_upgrade_flow_unchanged_without_a_profile_id(harness: AppHarness) -> None:
    # Radarr payloads lacking qualityProfileId behave exactly as before: app default wins.
    report_id = "report-20260814-120000-noprof"
    respx.get(f"{FIX_API}/movie").mock(return_value=httpx.Response(200, json=[{
        "tmdbId": 777, "id": 12, "title": "Neon Rain", "hasFile": True,
        "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}},
    }]))
    respx.get(f"{FIX_API}/qualityprofile").mock(return_value=httpx.Response(200, json=[
        {"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"},
    ]))
    respx.get(f"{FIX_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    harness.activate()
    harness.client.app.state.apps.add(name="Radarr", url=FIX_RADARR_URL, api_key=RADARR_KEY)
    harness.client.post(
        "/settings/filters",
        data={"quality_profile_id": "4", "default_root_folder": "/movies",
              "schedule_interval_hours": "168"},
        follow_redirects=False,
    )
    _save(harness, Report(
        id=report_id, run_at="2026-08-14T12:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain", gross_amount=1,
            gross_display="$0.0M", weeks_in_release=1, status=MovieStatus.IN_LIBRARY,
            action=MovieAction.NONE, tmdb_id=777,
        )],
    ))

    page = harness.client.get(f"/reports/{report_id}").text
    assert "profile:" not in page  # nothing to report
    assert '<option value="4" selected>HD-1080p</option>' in page  # app default preselected


# --- previous/next week navigation (F14) ---


def _week_report(harness: AppHarness, report_id: str, week: str) -> None:
    _save(harness, Report(
        id=report_id, run_at="2026-08-14T12:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week=week, totals=ReportTotals(movies=0, matched=0),
    ))


def test_adjacent_weeks_link_straight_to_each_other(harness: AppHarness) -> None:
    harness.activate()
    _week_report(harness, "report-20260814-120000-w26", "2026W26")
    _week_report(harness, "report-20260814-130000-w27", "2026W27")

    page = harness.client.get("/reports/report-20260814-130000-w27").text
    assert "/reports/report-20260814-120000-w26" in page  # one click back
    assert "← Week 2026W26" in page


def test_a_missing_neighbour_offers_to_fetch_it(harness: AppHarness) -> None:
    harness.activate()
    _week_report(harness, "report-20260814-120000-w26", "2026W26")

    page = harness.client.get("/reports/report-20260814-120000-w26").text
    assert "← Fetch 2026W25" in page  # no report for W25 yet — fetch on demand
    assert 'value="2026W25"' in page


def test_no_forward_control_beyond_the_current_week(harness: AppHarness) -> None:
    # Never offer to fetch a week that hasn't happened.
    harness.activate()
    this_week = bom_week_id(date.today())
    _week_report(harness, "report-20260814-140000-now", this_week)

    page = harness.client.get("/reports/report-20260814-140000-now").text
    assert "→" not in page  # no next-side control at all
    assert "← " in page  # but the previous week is still reachable


def test_navigation_absent_for_a_current_week_report(harness: AppHarness) -> None:
    # A report labelled "current" (the test mock's bare page) has no week arithmetic.
    harness.activate()
    _week_report(harness, "report-20260814-150000-cur", "current")
    page = harness.client.get("/reports/report-20260814-150000-cur").text
    assert "Fetch " not in page and "→" not in page


def _ranked_report(report_id: str, week: str, rank: int, gross: int) -> Report:
    return _ok_report(report_id).model_copy(
        update={
            "week": week,
            "movies": [_ok_report(report_id).movies[0].model_copy(update={"rank": rank,
                                                                         "gross_amount": gross})],
        }
    )


def test_detail_shows_the_week_by_week_trend(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ranked_report("report-20260814-100000-w26", "2026W26", 1, 45_000_000))
    _save(harness, _ranked_report("report-20260814-110000-w27", "2026W27", 4, 12_000_000))

    page = harness.client.get("/reports/report-20260814-110000-w27").text
    assert "W26 #1 → W27 #4" in page  # the title's chart run, oldest first


def test_a_first_week_title_has_no_trend(harness: AppHarness) -> None:
    # One data point is not a trend; don't clutter the card with it.
    harness.activate()
    _save(harness, _ranked_report("report-20260814-100000-solo", "2026W26", 1, 45_000_000))

    page = harness.client.get("/reports/report-20260814-100000-solo").text
    assert 'class="trend"' not in page


def test_schedule_line_is_absent_without_a_running_scheduler(harness: AppHarness) -> None:
    # A bare TestClient never ran the lifespan, so no job exists to report on.
    harness.activate()
    assert harness.client.app.state.scheduler is None
    page = harness.client.get("/reports").text
    assert "Next automatic check:" not in page


async def test_reports_page_shows_the_next_scheduled_run(harness: AppHarness) -> None:
    harness.activate()
    scheduler = BoxMediaScheduler(
        harness.client.app.state.pipeline,
        interval_hours=168,
        schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler.start()
    harness.client.app.state.scheduler = scheduler
    try:
        page = harness.client.get("/reports").text
        assert "Next automatic check:" in page
        assert "every 168h" in page
        # The exact moment APScheduler holds, formatted the way every other date is.
        assert format_timestamp(scheduler.next_run_at()) in page
    finally:
        scheduler.shutdown()
        harness.client.app.state.scheduler = None


async def test_a_cadence_schedule_names_its_days_instead_of_an_interval(
    harness: AppHarness,
) -> None:
    """M4: "every 168h" is simply untrue of a cadence that fires four times a week, and a
    line whose whole job is describing the schedule is the last place to leave one."""
    harness.activate()
    scheduler = BoxMediaScheduler(
        harness.client.app.state.pipeline,
        interval_hours=168,
        schedule_mode=SCHEDULE_MODE_CADENCE,
    )
    scheduler.start()
    harness.client.app.state.scheduler = scheduler
    try:
        page = harness.client.get("/reports").text
        assert "Next automatic check:" in page
        assert "Sun · Mon · Wed · Fri" in page
        assert "every" not in page.split("Next automatic check:")[1].split("</p>")[0]
    finally:
        scheduler.shutdown()
        harness.client.app.state.scheduler = None


def test_timestamps_render_day_first() -> None:
    # The app-wide guarantee the schedule line depends on: 17 August, never 8/17.
    moment = datetime(2026, 8, 17, 3, 12, tzinfo=UTC)
    assert format_timestamp(moment) == "17/8/2026 03:12"


BAD_WEEKS = [
    "../../../robots.txt",   # escapes /weekly/ once httpx normalizes the path
    "2026W31/../../x",
    "2026W99",               # right shape, not a real ISO week
    "2026W00",
    "not-a-week",
    "x" * 300,
]


@pytest.mark.parametrize("bad_week", BAD_WEEKS)
def test_a_crafted_week_is_rejected_before_it_is_stored_or_fetched(
    harness: AppHarness, bad_week: str
) -> None:
    """The week drives both the stored report label and the outbound chart URL path."""
    harness.activate()
    before = len(harness.client.app.state.reports.list_reports())

    response = harness.client.post(
        "/run", data={"week": bad_week}, follow_redirects=False
    )

    assert response.headers["location"].endswith("/reports?status=bad_week")
    assert len(harness.client.app.state.reports.list_reports()) == before  # nothing stored


def test_the_current_week_sentinel_is_still_accepted(harness: AppHarness) -> None:
    # reports.html sends week="current" for a card whose week never resolved; that must
    # keep meaning "run the current week", not fail validation.
    from app.web.reports import _resolve_week

    assert _resolve_week("current", "") is None
    assert _resolve_week("", "") is None


def test_a_real_week_id_still_resolves(harness: AppHarness) -> None:
    from app.web.reports import _resolve_week

    assert _resolve_week("2026W31", "") == "2026W31"
    assert _resolve_week("", "2026-08-14") == "2026W33"  # the date picker path


MALFORMED_IDS = [
    "not-a-report-id",
    "report-with-a-slash%2Fx",
    "report-ok..%2F..%2Fescape",
    "x" * 200,
]


@pytest.mark.parametrize("bad_id", MALFORMED_IDS)
def test_deleting_a_malformed_id_never_500s(harness: AppHarness, bad_id: str) -> None:
    """The contract is "no server error", not one specific code.

    An id carrying an encoded slash decodes to extra path segments, so routing rejects it
    with 404 before the handler is reached — earlier and just as correct. Everything that
    does reach the handler redirects.
    """
    harness.activate()
    response = harness.client.post(f"/reports/{bad_id}/delete", follow_redirects=False)
    assert response.status_code in (303, 404)
    if response.status_code == 303:
        assert response.headers["location"].endswith("/reports")


def test_deleting_a_malformed_id_is_not_audited(harness: AppHarness) -> None:
    """The Security page is only useful if every line in it happened."""
    harness.activate()
    harness.client.post("/reports/not-a-report-id/delete", follow_redirects=False)
    assert not any("report_deleted" in line for line in harness.audit_lines())


def test_deleting_an_absent_but_well_formed_id_is_not_audited(harness: AppHarness) -> None:
    harness.activate()
    harness.client.post(
        "/reports/report-20260101-000000-ffff/delete", follow_redirects=False
    )
    assert not any("report_deleted" in line for line in harness.audit_lines())


def test_deleting_a_real_report_still_works_and_is_audited(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ok_report("report-20260814-100000-dele"))

    response = harness.client.post(
        "/reports/report-20260814-100000-dele/delete", follow_redirects=False
    )
    assert response.status_code == 303
    assert harness.client.app.state.reports.list_reports() == []
    assert any("report_deleted" in line for line in harness.audit_lines())


def test_a_crafted_id_can_never_unlink_outside_the_history_directory(
    harness: AppHarness, tmp_path
) -> None:
    """The id check is a path guard, not just validation — swallowing the error must not
    turn it into a silent success."""
    import pytest as _pytest

    store = harness.client.app.state.reports
    victim = tmp_path / "important.json"
    victim.write_text("{}", encoding="utf-8")

    with _pytest.raises(ValueError):
        store.delete(f"../../../../{victim}")
    assert victim.exists()


def _mismatched_report(harness: AppHarness, report_id: str) -> None:
    """A row matched to the WRONG film — the case Wrong match? exists for.

    Mirrors the reported bug: the chart row was matched to a sequel, and correcting it
    left the heading naming one film while the poster showed another.
    """
    movie = MovieResult(
        rank=1, title="Toy Story 5", normalized_title="toy story 5",
        gross_amount=11_500_000, gross_display="$11.5M", weeks_in_release=7,
        status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=999,
        year=2026, poster_url="http://img/wrong.jpg",
        imdb_url="https://www.imdb.com/title/tt999/",
        wiki_url="https://en.wikipedia.org/wiki/Special:Search?search=Toy%20Story%205",
    )
    _save(harness, Report(
        id=report_id, run_at="2026-08-16T00:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W32", totals=ReportTotals(movies=1, matched=0),
        movies=[movie],
    ))
    harness.client.app.state.apps.add(name="Radarr", url=FIX_RADARR_URL, api_key=RADARR_KEY)


CORRECTED = [
    {"tmdbId": 301, "title": "Toy Story 4", "year": 2019, "overview": "The right one.",
     "images": [{"coverType": "poster", "remoteUrl": "http://img/ok.jpg"}], "imdbId": "tt301"},
]


def _correct_the_match(harness: AppHarness, report_id: str) -> None:
    respx.get(f"{FIX_API}/movie/lookup").mock(return_value=httpx.Response(200, json=CORRECTED))
    respx.get(f"{FIX_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{FIX_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{FIX_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    respx.get("http://img/ok.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg")
    )
    harness.client.post(
        "/fix-match",
        data={"report_id": report_id, "rank": "1", "term": "Toy Story", "tmdb_id": "301"},
        follow_redirects=False,
    )


@respx.mock
def test_a_corrected_match_updates_the_displayed_title(harness: AppHarness) -> None:
    """The reported symptom: poster showed Toy Story 4, heading still said Toy Story 5."""
    report_id = "report-20260816-000000-wm01"
    harness.activate()
    _mismatched_report(harness, report_id)
    _correct_the_match(harness, report_id)

    page = harness.client.get(f"/reports/{report_id}").text
    assert "Toy Story 4" in page
    assert "Toy Story 5" not in page  # the old identity is gone from the card


@respx.mock
def test_a_corrected_match_leaves_no_field_pointing_at_the_old_film(
    harness: AppHarness,
) -> None:
    """Every field derived from the title has to move together, or the row describes two
    different films at once."""
    report_id = "report-20260816-000000-wm02"
    harness.activate()
    _mismatched_report(harness, report_id)
    _correct_the_match(harness, report_id)

    stored = harness.client.app.state.reports.get(report_id).movies[0]
    assert stored.title == "Toy Story 4"
    assert stored.tmdb_id == 301
    assert stored.year == 2019
    assert stored.imdb_url == "https://www.imdb.com/title/tt301/"
    assert stored.wiki_url is not None and "Toy%20Story%204" in stored.wiki_url
    assert "Toy%20Story%205" not in stored.wiki_url


@respx.mock
def test_a_correction_does_not_move_the_row_out_from_under_the_chart(
    harness: AppHarness,
) -> None:
    """`normalized_title` is the identity of the CHART ROW, not of the film.

    Three things read it: the week's dedupe fingerprint, the ignore list, and the
    cross-week grouping that draws the trend line and folds the dashboard. It used to be
    re-derived from the corrected title, and all three broke at once — the week looked
    changed to the next automatic check, which overwrote the correction with a fresh
    guess, and the film's history split in two on the way. An identified row is ignored by
    tmdb id, so the ignore list does not need this to follow the display title.
    """
    report_id = "report-20260816-000000-wm03"
    harness.activate()
    _mismatched_report(harness, report_id)
    _correct_the_match(harness, report_id)

    stored = harness.client.app.state.reports.get(report_id).movies[0]
    assert stored.title == "Toy Story 4"          # what the card shows
    assert stored.normalized_title == "toy story 5"  # which chart row it is


@respx.mock
def test_the_add_form_posts_the_corrected_title_to_radarr(harness: AppHarness) -> None:
    # The card's hidden `title` field is what Add to Radarr sends; a stale one would ask
    # Radarr to add the right id under the wrong name.
    report_id = "report-20260816-000000-wm04"
    harness.activate()
    _mismatched_report(harness, report_id)
    _correct_the_match(harness, report_id)

    page = harness.client.get(f"/reports/{report_id}").text
    assert '<input type="hidden" name="title" value="Toy Story 4">' in page


# --- the weekly-view Radarr actions are time-bounded (review step 12) ---

HANG_SECONDS = 30.0
# The real bound is RADARR_ACTION_TIMEOUT_SECONDS (15s); shortened under test so proving
# it exists costs a fraction of a second instead of fifteen.
SHORT_BOUND_SECONDS = 0.3


async def _never_answers(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
    await asyncio.sleep(HANG_SECONDS)
    return httpx.Response(200, json=[])


def _bound(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        report_routes, "RADARR_ACTION_TIMEOUT_SECONDS", SHORT_BOUND_SECONDS
    )


@respx.mock
async def test_a_hung_add_gives_up_and_reports_failure(
    harness: AppHarness, monkeypatch  # noqa: ANN001
) -> None:
    """A powered-off Radarr whose name no longer resolves used to hold the request for
    the OS resolver's timeout — httpx's own timeout does not cover name resolution."""
    _bound(monkeypatch)
    harness.activate()
    app = harness.client.app.state.apps.add(
        name="Main", url=STEP12_URL, api_key=STEP12_KEY
    )
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=4, root_folder="/movies"
    )
    respx.get(f"{STEP12_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{STEP12_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    respx.get(f"{STEP12_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{STEP12_API}/movie").mock(side_effect=_never_answers)

    started = time.perf_counter()
    response = harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II"},
        follow_redirects=False,
    )
    elapsed = time.perf_counter() - started

    assert "status=add_failed" in response.headers["location"]
    assert elapsed < HANG_SECONDS / 2  # bounded, not waiting the host out


@respx.mock
async def test_a_hung_upgrade_gives_up_and_reports_failure(
    harness: AppHarness, monkeypatch  # noqa: ANN001
) -> None:
    _bound(monkeypatch)
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=STEP12_URL, api_key=STEP12_KEY)
    # Nothing cached and nothing answering means an empty profile list, which the route
    # treats as "cannot check" rather than "known wrong" — so it still reaches the hang.
    respx.get(f"{STEP12_API}/qualityprofile").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{STEP12_API}/rootfolder").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{STEP12_API}/movie/7").mock(side_effect=_never_answers)

    started = time.perf_counter()
    response = harness.client.post(
        "/upgrade-movie",
        data={"radarr_id": "7", "quality_profile_id": "5"},
        follow_redirects=False,
    )
    elapsed = time.perf_counter() - started

    assert "status=add_failed" in response.headers["location"]
    assert elapsed < HANG_SECONDS / 2


@respx.mock
async def test_a_hung_wrong_match_search_gives_up(
    harness: AppHarness, monkeypatch  # noqa: ANN001
) -> None:
    # The Wrong Match search is a GET the user sits and waits on with no way to cancel.
    _bound(monkeypatch)
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=STEP12_URL, api_key=STEP12_KEY)
    respx.get(f"{STEP12_API}/movie/lookup").mock(side_effect=_never_answers)

    started = time.perf_counter()
    response = harness.client.get(
        "/reports/report-20260812-100000-x/fix-match",
        params={"rank": "1", "term": "Dune"},
        follow_redirects=False,
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 303
    assert "status=add_failed" in response.headers["location"]
    assert elapsed < HANG_SECONDS / 2


# --- the last-run error is a toast like every other transient message ---


def _failed_run(harness: AppHarness) -> None:
    _save(harness, Report(
        id="report-20260816-100000-fail", run_at="2026-08-16T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.SCRAPE_FAILED, week="2026W33",
        totals=ReportTotals(movies=0, matched=0),
        error="No box-office data available for week 2026W33.",
    ))


def test_the_last_run_error_is_a_toast_not_a_page_banner(harness: AppHarness) -> None:
    """It used to sit full-width at the top of the page until the next successful run.
    Every other transient message in the app is a corner toast that fades itself out."""
    harness.activate()
    _failed_run(harness)

    page = harness.client.get("/reports").text
    toast = page.split('class="toast-region"')[1].split("</div>")[0]

    assert "Last run didn’t complete" in toast
    assert "data-toast" in toast          # so app.js clears it on the shared timer
    assert "banner-error" in toast        # still red
    assert "banner-warn" not in page      # the old inline treatment is gone


def test_the_failed_run_keeps_its_status_on_its_card(harness: AppHarness) -> None:
    """What makes fading the toast safe: the report itself keeps the failure, so the
    information is not lost with the message."""
    harness.activate()
    _failed_run(harness)

    page = harness.client.get("/reports").text
    grid = page.split('class="reports-grid"')[1]

    assert "scrape_failed" in grid  # stamped on the card, for as long as it is retained


def test_a_successful_run_shows_no_error_toast(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _ok_report("report-20260816-110000-good"))

    page = harness.client.get("/reports").text

    assert "Last run didn’t complete" not in page


def test_a_bad_date_and_a_failed_run_both_toast_together(harness: AppHarness) -> None:
    """Two transient messages can co-occur; the region stacks them and app.js clears
    all of them, not just the first."""
    harness.activate()
    _failed_run(harness)

    page = harness.client.get("/reports?status=bad_week").text
    toast = page.split('class="toast-region"')[1].split("</section>")[0]

    assert page.count("data-toast") == 2
    assert "pick a week with the date field" in toast
    assert "Last run didn’t complete" in toast


# --- a week is stored once: the second attempt says so instead of duplicating it ---


def _wire_stable_chart(harness: AppHarness, week: str = "2026W02") -> None:
    """A Radarr that adds nothing and a chart that never changes."""
    harness.client.post(
        "/settings/apps",
        data={"name": "Radarr", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )
    pipeline = harness.client.app.state.pipeline
    pipeline._make_radarr = lambda _app_id: _NonAddingRadarr()  # noqa: SLF001

    async def _chart(_week: str | None = None) -> tuple[str, list[BoxOfficeEntry]]:
        return (week, [BoxOfficeEntry(rank=1, title="Neon Rain",
                                      gross_amount=1_000_000, weeks_in_release=1)])

    pipeline._fetch_chart = _chart  # noqa: SLF001


def test_running_an_unchanged_week_lands_on_the_report_that_already_exists(
    harness: AppHarness,
) -> None:
    """Not a silent no-op and not a duplicate: the same report, and a line saying why
    there is nothing new."""
    harness.activate()
    _wire_stable_chart(harness)

    first = harness.client.post("/run", follow_redirects=False)
    again = harness.client.post("/run", follow_redirects=False)

    assert first.headers["location"] == again.headers["location"].split("?")[0]
    assert "status=unchanged" in again.headers["location"]
    assert len(harness.client.app.state.reports.list_reports()) == 1


def test_the_unchanged_message_reaches_the_page(harness: AppHarness) -> None:
    harness.activate()
    _wire_stable_chart(harness)
    harness.client.post("/run", follow_redirects=False)

    page = harness.client.post("/run", follow_redirects=True).text

    assert "Already up to date" in page


def test_a_first_run_carries_no_unchanged_message(harness: AppHarness) -> None:
    # The banner must mean something: it cannot appear on a week being stored for the
    # first time.
    harness.activate()
    _wire_stable_chart(harness)

    response = harness.client.post("/run", follow_redirects=False)

    assert "status=unchanged" not in response.headers["location"]


def test_a_week_fetched_again_stays_one_card(harness: AppHarness) -> None:
    """The reported bug: two cards reading "Week 2026W32 · manual". A week is a week —
    the numbers may change, the week number cannot."""
    harness.activate()
    _wire_stable_chart(harness, week="2026W32")
    harness.client.post("/run", follow_redirects=False)

    # A revised chart for the same week: different numbers, same week.
    pipeline = harness.client.app.state.pipeline

    async def _revised(_week: str | None = None) -> tuple[str, list[BoxOfficeEntry]]:
        return ("2026W32", [
            BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=9_999_999,
                           weeks_in_release=1),
        ])

    pipeline._fetch_chart = _revised  # noqa: SLF001
    harness.client.post("/run", follow_redirects=False)

    reports = harness.client.app.state.reports.list_reports()
    assert len(reports) == 1
    assert reports[0].week == "2026W32"
    assert reports[0].movies[0].gross_amount == 9_999_999  # the fresher figures


def test_a_refreshed_week_keeps_the_link_that_points_at_it(harness: AppHarness) -> None:
    """The week chips on the search results and the ignored list link to a report id. An
    in-place refresh must not break every one of them."""
    harness.activate()
    _wire_stable_chart(harness, week="2026W32")
    first = harness.client.post("/run", follow_redirects=False)
    report_id = first.headers["location"].rsplit("/", 1)[1].split("?")[0]

    pipeline = harness.client.app.state.pipeline

    async def _revised(_week: str | None = None) -> tuple[str, list[BoxOfficeEntry]]:
        return ("2026W32", [BoxOfficeEntry(rank=1, title="Neon Rain",
                                           gross_amount=2_000_000, weeks_in_release=1)])

    pipeline._fetch_chart = _revised  # noqa: SLF001
    harness.client.post("/run", follow_redirects=False)

    assert harness.client.get(f"/reports/{report_id}").status_code == 200


def test_the_grid_lists_weeks_newest_first(harness: AppHarness) -> None:
    """Ordering by fetch time would send a refreshed old week to the front the moment
    Mojo revised its figures."""
    harness.activate()
    store = harness.client.app.state.reports
    for week, run_at in (("2026W30", "2026-08-14T10:00:00+00:00"),   # fetched last
                         ("2026W32", "2026-08-11T10:00:00+00:00"),
                         ("2026W31", "2026-08-12T10:00:00+00:00")):
        store.save(Report(
            id=f"report-{week}", run_at=run_at, trigger=RunTrigger.MANUAL,
            status=RunStatus.OK, week=week, totals=ReportTotals(movies=1, matched=0),
        ))

    page = harness.client.get("/reports").text
    # Anchored on the card's own line: "Week to fetch" is the date input's label.
    order = [chunk[:7] for chunk in page.split('class="report-meta">Week ')[1:]]

    assert order == ["2026W32", "2026W31", "2026W30"]


# --- the schedule's track record, beside its promise ---


def _scheduled_report(harness: AppHarness, run_at: str, week: str = "2026W30") -> None:
    harness.client.app.state.reports.save(Report(
        id=f"report-{week}-sched", run_at=run_at, trigger=RunTrigger.SCHEDULED,
        status=RunStatus.OK, week=week, totals=ReportTotals(movies=1, matched=0),
    ))


def _with_scheduler(harness: AppHarness) -> BoxMediaScheduler:
    scheduler = BoxMediaScheduler(
        harness.client.app.state.pipeline, interval_hours=168,
        reports=harness.client.app.state.reports,
    )
    scheduler.start()
    harness.client.app.state.scheduler = scheduler
    return scheduler


async def test_the_page_names_when_the_check_last_ran(harness: AppHarness) -> None:
    """The fact this line exists for: a schedule that shows only a next date cannot tell
    you it has never actually fired."""
    harness.activate()
    _scheduled_report(harness, "2026-08-12T14:03:00+00:00")
    scheduler = _with_scheduler(harness)
    try:
        page = harness.client.get("/reports").text

        assert "Last check:" in page
        assert "12/8/2026 14:03" in page  # day-first, as every date in the app reads
        assert "Next automatic check:" in page
    finally:
        scheduler.shutdown()


async def test_a_schedule_that_has_never_fired_says_never(harness: AppHarness) -> None:
    """Not format_timestamp's "—", which reads as a time we do not know. A run that has
    not happened is a different fact, and the one worth noticing."""
    harness.activate()
    scheduler = _with_scheduler(harness)
    try:
        page = harness.client.get("/reports").text

        assert f"Last check: {NEVER_RAN}" in page
    finally:
        scheduler.shutdown()


async def test_a_manual_run_is_not_a_scheduled_check(harness: AppHarness) -> None:
    """Only the unattended job counts. Manual runs are the admin standing there — they
    say nothing about whether the schedule is alive."""
    harness.activate()
    harness.client.app.state.reports.save(Report(
        id="report-2026W31-manual", run_at="2026-08-13T09:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W31",
        totals=ReportTotals(movies=1, matched=0),
    ))
    scheduler = _with_scheduler(harness)
    try:
        page = harness.client.get("/reports").text

        assert f"Last check: {NEVER_RAN}" in page
        assert "13/8/2026 09:00" not in page
    finally:
        scheduler.shutdown()


async def test_a_failed_scheduled_run_still_counts_as_a_check(harness: AppHarness) -> None:
    """It went to Mojo and came back empty-handed — which is exactly what the admin needs
    to see, and what keeps a broken scraper from being retried on every restart."""
    harness.activate()
    harness.client.app.state.reports.save(Report(
        id="report-2026W31-failed", run_at="2026-08-14T11:30:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED, week="2026W31",
        totals=ReportTotals(movies=0, matched=0), error="layout changed",
    ))
    scheduler = _with_scheduler(harness)
    try:
        page = harness.client.get("/reports").text

        assert "14/8/2026 11:30" in page
        assert NEVER_RAN not in page.split("Next automatic check:")[0]
    finally:
        scheduler.shutdown()


async def test_the_newest_scheduled_run_is_the_one_shown(harness: AppHarness) -> None:
    harness.activate()
    _scheduled_report(harness, "2026-07-20T08:00:00+00:00", week="2026W29")
    _scheduled_report(harness, "2026-08-12T14:03:00+00:00", week="2026W30")
    scheduler = _with_scheduler(harness)
    try:
        page = harness.client.get("/reports").text

        assert "12/8/2026 14:03" in page
        assert "20/7/2026 08:00" not in page
    finally:
        scheduler.shutdown()


async def test_the_page_reads_the_history_once(harness: AppHarness) -> None:
    """The last-run answer comes from the list the page already loaded, following the
    same convention ReportsStore.histories and completed_weeks use. Asking the store
    again would double a read that runs on every view of this page."""
    harness.activate()
    _scheduled_report(harness, "2026-08-12T14:03:00+00:00")
    scheduler = _with_scheduler(harness)
    store = harness.client.app.state.reports
    reads = 0
    original = store.list_reports

    def counted(*args: object, **kwargs: object) -> list[Report]:
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    store.list_reports = counted  # type: ignore[method-assign]
    try:
        harness.client.get("/reports")
    finally:
        store.list_reports = original  # type: ignore[method-assign]
        scheduler.shutdown()

    assert reads == 1, f"the history directory was read {reads} times for one page"


# --- the trend line labels its weeks the same way ---


def _charted(harness: AppHarness, week: str, rank: int) -> None:
    harness.client.app.state.reports.save(Report(
        id=f"report-{week}-t", run_at=f"20{week[2:4]}-01-01T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
        totals=ReportTotals(movies=1, matched=0),
        movies=[MovieResult(
            rank=rank, title="Long Runner", normalized_title="long runner",
            gross_amount=1_000_000, gross_display="$1.0M", weeks_in_release=rank,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=99)]))


async def test_a_trend_crossing_new_year_names_the_years(harness: AppHarness) -> None:
    """A December release charts into January. Without the year the line claims the film
    went from week 52 to week 1 — the same ambiguity as the chips, on the same page."""
    harness.activate()
    _charted(harness, "2025W52", 3)
    _charted(harness, "2026W01", 1)

    page = harness.client.get("/reports/report-2026W01-t").text
    trend = page.split('class="trend"')[1].split("</span>")[0]

    assert "W52 ’25" in trend
    assert "W01 ’26" in trend


async def test_a_trend_within_one_year_stays_compact(harness: AppHarness) -> None:
    harness.activate()
    _charted(harness, "2026W01", 3)
    _charted(harness, "2026W02", 1)

    page = harness.client.get("/reports/report-2026W02-t").text
    trend = page.split('class="trend"')[1].split("</span>")[0]

    assert "W01 #3" in trend and "W02 #1" in trend
    assert "’" not in trend


# --- the card shows what the film is, not only what it earned ---


def _rated_report(harness: AppHarness, rating: float | None, genres: list[str]) -> None:
    harness.client.app.state.reports.save(Report(
        id="report-2026W02-rated", run_at="2026-01-09T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W02",
        totals=ReportTotals(movies=1, matched=0),
        movies=[MovieResult(
            rank=1, title="Skin Crawl", normalized_title="skin crawl",
            gross_amount=5_800_000, gross_display="$5.8M", weeks_in_release=7,
            rating=rating, genres=genres,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=666)]))


async def test_the_card_shows_the_rating_and_genres(harness: AppHarness) -> None:
    harness.activate()
    _rated_report(harness, 7.2, ["Horror", "Thriller"])

    card = harness.client.get("/reports/report-2026W02-rated").text

    assert "★ 7.2" in card
    assert "Horror · Thriller" in card
    assert "$5.8M (Wk 7)" in card  # what it earned is still there


async def test_a_rating_is_shown_to_one_decimal(harness: AppHarness) -> None:
    harness.activate()
    _rated_report(harness, 7.0, [])

    assert "★ 7.0" in harness.client.get("/reports/report-2026W02-rated").text


async def test_an_unrated_film_shows_no_star(harness: AppHarness) -> None:
    """Radarr has no score for plenty of titles. An empty star, or a bare 0.0, would be a
    claim the data does not support."""
    harness.activate()
    _rated_report(harness, None, ["Documentary"])

    card = harness.client.get("/reports/report-2026W02-rated").text

    assert "★" not in card
    assert "Documentary" in card


async def test_a_film_with_neither_shows_no_line_at_all(harness: AppHarness) -> None:
    """Every report written before this existed. The row appears only when there is
    something to put in it."""
    harness.activate()
    _rated_report(harness, None, [])

    card = harness.client.get("/reports/report-2026W02-rated").text

    assert "card-facts" not in card


async def test_the_facts_line_carries_the_full_text_as_a_tooltip(
    harness: AppHarness,
) -> None:
    """The line truncates rather than wraps — the cards sit in a fixed grid and a second
    line would make one card taller than its row — so the whole value stays reachable."""
    harness.activate()
    _rated_report(harness, 8.4, ["Science Fiction", "Adventure", "Drama"])

    card = harness.client.get("/reports/report-2026W02-rated").text

    assert 'title="Rated 8.4 · Science Fiction · Adventure · Drama"' in card


# --- fix-match shows the artwork and score it was already given ---

FIX_LOOKUP_URL = f"{FIX_API}/movie/lookup"


def _fix_candidates(*, with_poster: bool = True) -> list[dict]:
    """Two films sharing a title — the case this page exists for."""
    art = [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/a.jpg"}]
    return [
        {"tmdbId": 111, "title": "Cookie Queens", "year": 2026,
         "overview": "The real one.", "images": art if with_poster else [],
         "genres": ["Comedy"], "imdbId": "tt111", "ratings": {"tmdb": {"value": 7.2}}},
        {"tmdbId": 222, "title": "Cookie Queens", "year": 1998,
         "overview": "The remake's namesake.", "images": [],
         "genres": ["Drama"], "imdbId": "tt222", "ratings": {}},
    ]


@respx.mock
def test_fix_match_shows_a_poster_for_a_candidate_that_has_one(
    harness: AppHarness,
) -> None:
    """Choosing between two same-titled films by prose alone is the mistake this page
    exists to fix, and the lookup was already returning the artwork."""
    report_id = "report-20260814-110000-fixart"
    respx.get(FIX_LOOKUP_URL).mock(return_value=httpx.Response(200, json=_fix_candidates()))
    respx.get(url__regex=r"https://image\.tmdb\.org/.*").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg"))
    harness.activate()
    _no_match_report(harness, report_id)

    page = harness.client.get(
        f"/reports/{report_id}/fix-match", params={"rank": 1, "term": "Cookie Queens"}
    ).text

    assert "/posters/" in page          # served locally, so img-src 'self' holds
    assert "image.tmdb.org" not in page  # never hot-linked
    assert "★ 7.2" in page


@respx.mock
def test_a_candidate_without_artwork_falls_back_instead_of_breaking(
    harness: AppHarness,
) -> None:
    """A film Radarr has no poster for must render the placeholder, not a broken image."""
    report_id = "report-20260814-110000-fixnoart"
    respx.get(FIX_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_fix_candidates(with_poster=False)))
    harness.activate()
    _no_match_report(harness, report_id)

    page = harness.client.get(
        f"/reports/{report_id}/fix-match", params={"rank": 1, "term": "Cookie Queens"}
    ).text

    assert page.count('class="poster-empty"') == 2
    assert "<img" not in page.split('class="detail-table"')[1]


@respx.mock
def test_an_unrated_candidate_shows_no_star(harness: AppHarness) -> None:
    """The 1998 film in the fixture has no ratings at all; a bare 0.0 would be a claim
    Radarr never made."""
    report_id = "report-20260814-110000-fixrate"
    respx.get(FIX_LOOKUP_URL).mock(return_value=httpx.Response(200, json=_fix_candidates()))
    respx.get(url__regex=r"https://image\.tmdb\.org/.*").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg"))
    harness.activate()
    _no_match_report(harness, report_id)

    page = harness.client.get(
        f"/reports/{report_id}/fix-match", params={"rank": 1, "term": "Cookie Queens"}
    ).text

    assert page.count("★") == 1  # only the rated candidate


@respx.mock
def test_choosing_still_works_with_the_new_columns(harness: AppHarness) -> None:
    """The action this page is for must survive its own redesign."""
    report_id = "report-20260814-110000-fixuse"
    respx.get(FIX_LOOKUP_URL).mock(return_value=httpx.Response(200, json=_fix_candidates()))
    respx.get(url__regex=r"https://image\.tmdb\.org/.*").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpg"))
    harness.activate()
    _no_match_report(harness, report_id)

    response = harness.client.post(
        "/fix-match",
        data={"report_id": report_id, "rank": "1", "term": "Cookie Queens", "tmdb_id": "111"},
        follow_redirects=False,
    )

    assert "status=match_fixed" in response.headers["location"]
    assert harness.client.app.state.reports.get(report_id).movies[0].tmdb_id == 111


# --- a guessed match says it guessed ---


def _guessed_report(harness: AppHarness, detail: str | None, *, in_library: bool = False) -> str:
    report_id = "report-2026W02-guess"
    status = MovieStatus.IN_LIBRARY if in_library else MovieStatus.MISSING
    harness.client.app.state.reports.save(Report(
        id=report_id, run_at="2026-01-09T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W02",
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=5_800_000, gross_display="$5.8M", weeks_in_release=2,
            status=status, action=MovieAction.NONE, tmdb_id=777, detail=detail)]))
    return report_id


async def test_a_guessed_card_says_so_and_opens_the_fix(harness: AppHarness) -> None:
    """The card's whole claim rests on a match nothing verified. Saying so is the
    difference between finding out here and finding out when the poster looks wrong."""
    harness.activate()
    report_id = _guessed_report(harness, MATCHED_BY_GUESS)

    card = harness.client.get(f"/reports/{report_id}").text

    assert "guess-hint" in card
    assert "Best guess — verify" in card
    assert "<details open>" in card, "the fix is offered open, not one click away"


async def test_an_ordinary_card_is_unchanged(harness: AppHarness) -> None:
    harness.activate()
    report_id = _guessed_report(harness, None)

    card = harness.client.get(f"/reports/{report_id}").text

    assert "guess-hint" not in card
    assert "Wrong match?" in card  # still offered — just closed, as before
    assert "<details open>" not in card


async def test_a_lookup_error_is_not_mistaken_for_a_guess(harness: AppHarness) -> None:
    """`detail` already carried Radarr failures. Only the one marker means guessed."""
    harness.activate()
    report_id = _guessed_report(harness, "lookup failed: connection refused")

    assert "guess-hint" not in harness.client.get(f"/reports/{report_id}").text


async def test_a_guess_the_admin_already_owns_shows_no_hint(harness: AppHarness) -> None:
    """The report still records it, but a film in the library has no fix-match control —
    a warning with nothing to act on is just noise."""
    harness.activate()
    report_id = _guessed_report(harness, MATCHED_BY_GUESS, in_library=True)

    card = harness.client.get(f"/reports/{report_id}").text

    assert "guess-hint" not in card
    assert harness.client.app.state.reports.get(report_id).movies[0].detail == MATCHED_BY_GUESS


# --- filling the holes in stored history (F19) ---


def _seed_weeks(harness: AppHarness, weeks: list[str]) -> None:
    for week in weeks:
        harness.client.app.state.reports.save(Report(
            id=f"report-{week}", run_at="2026-08-14T10:00:00+00:00",
            trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
            totals=ReportTotals(movies=0, matched=0),
        ))


def _recording_runner(harness: AppHarness) -> list[list[str]]:
    """Swap the runner for one that records what the route hands it.

    The route's job is to derive the weeks from stored history and pass them on; the loop
    that fetches them is an in-process asyncio task on TestClient's own event loop, which
    cannot be awaited from out here. So the loop is tested where it can be — in
    tests/unit/test_backfill.py, against the same BackfillRunner — and this asserts the
    half that is the route's.
    """
    handed: list[list[str]] = []

    class _Recorder:
        def status(self) -> dict[str, object]:
            return {"running": False, "done": 0, "total": 0, "current_week": None}

        def start(self, weeks: list[str]) -> bool:
            handed.append(list(weeks))
            return True

    harness.client.app.state.backfill = _Recorder()
    return handed


def test_the_page_names_the_missing_weeks_and_offers_to_fetch_them(
    harness: AppHarness,
) -> None:
    """A gap used to mean a date pick and a POST per week, with nothing telling you one
    was there."""
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W33"])

    page = harness.client.get("/reports").text

    assert "2 weeks missing between W31 and W32" in page
    assert "W31, W32" in page
    assert "Fetch missing weeks" in page
    assert "/run-backfill" in page


def test_a_contiguous_history_offers_nothing(harness: AppHarness) -> None:
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W31", "2026W32"])

    page = harness.client.get("/reports").text

    assert "Fetch missing weeks" not in page
    assert "missing between" not in page


def test_the_button_hands_over_every_gap_oldest_first(harness: AppHarness) -> None:
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W34"])
    handed = _recording_runner(harness)

    response = harness.client.post("/run-backfill", follow_redirects=False)

    assert response.headers["location"].endswith("/reports")
    assert handed == [["2026W31", "2026W32", "2026W33"]]


def test_the_request_carries_no_weeks_at_all(harness: AppHarness) -> None:
    """The list is read from stored history inside the handler, so nothing a client sends
    can steer which weeks get fetched or how many — the same rule the add route follows
    for connection ids."""
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W32"])
    handed = _recording_runner(harness)

    harness.client.post(
        "/run-backfill",
        data={"weeks": "2020W01,2020W02", "count": "999"},
        follow_redirects=False,
    )

    assert handed == [["2026W31"]]


def test_the_page_shows_progress_while_it_runs(harness: AppHarness) -> None:
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W33"])

    class _Frozen:
        running = True

        def status(self) -> dict[str, object]:
            return {"running": True, "done": 1, "total": 2, "current_week": "2026W32"}

        def start(self, weeks: list[str]) -> bool:
            return False

    harness.client.app.state.backfill = _Frozen()

    page = harness.client.get("/reports").text

    assert "Backfilling week 2026W32 — 1 of 2 fetched" in page
    # ...and the offer is not shown beside it, which would be two answers to one question.
    assert "Fetch missing weeks" not in page


def test_a_week_that_failed_is_not_offered_again(harness: AppHarness) -> None:
    """Mojo has no data for some weeks. Offering that hole forever would re-fetch it on
    every backfill — politely, and endlessly."""
    harness.activate()
    _seed_weeks(harness, ["2026W30", "2026W32"])
    harness.client.app.state.reports.save(Report(
        id="report-failed-31", run_at="2026-08-14T11:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.SCRAPE_FAILED, week="2026W31",
        totals=ReportTotals(movies=0, matched=0), error="no data for that week",
    ))

    page = harness.client.get("/reports").text

    assert "Fetch missing weeks" not in page


# --- the card says how many screens, and which way it moved (M3) ---


def _chart_facts_text(page: str) -> str:
    """The chart-facts line's own text, without its tooltip.

    Asserting against the whole page cannot tell the two apart — the title attribute
    repeats the same figures — so a change to the visible line alone passed unnoticed.
    """
    if "chart-facts" not in page:
        return ""
    element = page.split('class="chart-facts"', 1)[1]
    return element.split(">", 1)[1].split("</span>", 1)[0].strip()


def _chart_row(harness: AppHarness, theaters: int | None, change: int | None) -> str:
    report_id = "report-2026W02-chart"
    harness.client.app.state.reports.save(Report(
        id=report_id, run_at="2026-01-09T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W02",
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=5_800_000, gross_display="$5.8M", weeks_in_release=2,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=555,
            theaters=theaters, gross_change_pct=change)]))
    return report_id


async def test_the_card_shows_the_screen_count_and_the_move(harness: AppHarness) -> None:
    """A collapse on four thousand screens and a hold on two hundred are opposite
    propositions, and the gross alone does not distinguish them."""
    harness.activate()
    report_id = _chart_row(harness, 4071, -38)

    card = harness.client.get(f"/reports/{report_id}").text

    assert "chart-facts" in card
    line = _chart_facts_text(card)
    # Grouped, so a four-figure count reads at a glance; the arrow carries the sign, so
    # the number itself is unsigned. Read off the LINE, not the page — the tooltip repeats
    # the figures and would answer for it.
    assert line == "4,071 theaters · ▼ 38% vs LW"


async def test_a_climbing_week_points_the_other_way(harness: AppHarness) -> None:
    harness.activate()
    report_id = _chart_row(harness, 2100, 12)

    card = harness.client.get(f"/reports/{report_id}").text

    assert _chart_facts_text(card) == "2,100 theaters · ▲ 12% vs LW"


async def test_a_flat_week_is_shown_as_flat_not_as_missing(harness: AppHarness) -> None:
    """0% is a fact Mojo reported; None is Mojo having nothing to compare against. The
    line must tell them apart, which `if movie.gross_change_pct` would not."""
    harness.activate()
    report_id = _chart_row(harness, 2100, 0)

    line = _chart_facts_text(harness.client.get(f"/reports/{report_id}").text)
    assert line == "2,100 theaters · ▲ 0% vs LW"


async def test_a_first_week_shows_screens_without_inventing_a_move(
    harness: AppHarness,
) -> None:
    harness.activate()
    report_id = _chart_row(harness, 4071, None)

    card = harness.client.get(f"/reports/{report_id}").text

    assert _chart_facts_text(card) == "4,071 theaters"


async def test_a_report_written_before_these_existed_renders_no_line(
    harness: AppHarness,
) -> None:
    """Every stored week from before this shipped. The row appears only when there is
    something to put in it."""
    harness.activate()
    report_id = _chart_row(harness, None, None)

    assert "chart-facts" not in harness.client.get(f"/reports/{report_id}").text


async def test_the_line_carries_the_full_text_as_a_tooltip(harness: AppHarness) -> None:
    """It truncates rather than wraps — the cards sit in a fixed grid and a second line
    would make one card taller than its row — so the whole value stays reachable."""
    harness.activate()
    report_id = _chart_row(harness, 4071, -38)

    card = harness.client.get(f"/reports/{report_id}").text

    assert '4,071 theaters · -38% against last week"' in card


# --- a correction is a fact about a chart title, not about one row ---


def _charted_in(harness: AppHarness, report_id: str, week: str, rank: int) -> None:
    """The same film, the same chart title, in another week."""
    _save(harness, Report(
        id=report_id, run_at=f"2026-08-{rank:02d}T00:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.OK, week=week,
        totals=ReportTotals(movies=1, matched=0),
        movies=[MovieResult(
            rank=rank, title="Toy Story 5", normalized_title="toy story 5",
            gross_amount=11_500_000, gross_display="$11.5M", weeks_in_release=7,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=999,
            year=2026, poster_url="http://img/wrong.jpg",
            imdb_url="https://www.imdb.com/title/tt999/",
            detail=MATCHED_BY_GUESS,
        )],
    ))


@respx.mock
def test_correcting_one_week_corrects_every_week_that_title_charted(
    harness: AppHarness,
) -> None:
    """The reported complaint: the same film runs for weeks, and fixing the week you
    happen to be looking at left every other week showing the wrong film."""
    report_id = "report-20260816-000000-wm05"
    harness.activate()
    _mismatched_report(harness, report_id)
    _charted_in(harness, "report-20260809-000000-wm05", "2026W31", 2)
    _charted_in(harness, "report-20260802-000000-wm05", "2026W30", 3)

    _correct_the_match(harness, report_id)

    for other in ("report-20260809-000000-wm05", "report-20260802-000000-wm05"):
        stored = harness.client.app.state.reports.get(other).movies[0]
        assert stored.tmdb_id == 301, other
        assert stored.title == "Toy Story 4", other
        assert stored.imdb_url == "https://www.imdb.com/title/tt301/", other


@respx.mock
def test_a_correction_clears_the_guess_hint_it_answers(harness: AppHarness) -> None:
    """The amber marker says nothing verified this. Someone just did."""
    report_id = "report-20260816-000000-wm06"
    harness.activate()
    _charted_in(harness, report_id, "2026W32", 1)
    harness.client.app.state.apps.add(name="Radarr", url=FIX_RADARR_URL, api_key=RADARR_KEY)

    _correct_the_match(harness, report_id)

    assert harness.client.app.state.reports.get(report_id).movies[0].detail is None
    assert "guess-hint" not in harness.client.get(f"/reports/{report_id}").text


@respx.mock
def test_a_different_film_on_another_week_is_left_alone(harness: AppHarness) -> None:
    """Keyed by the chart title, so a correction reaches exactly the rows it is about."""
    report_id = "report-20260816-000000-wm07"
    harness.activate()
    _mismatched_report(harness, report_id)
    _save(harness, Report(
        id="report-20260809-000000-wm07", run_at="2026-08-09T00:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.OK, week="2026W31",
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Skin Crawl", normalized_title="skin crawl",
            gross_amount=1, gross_display="$0.0M", weeks_in_release=1,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=888)],
    ))

    _correct_the_match(harness, report_id)

    untouched = harness.client.app.state.reports.get("report-20260809-000000-wm07").movies[0]
    assert untouched.tmdb_id == 888
    assert untouched.title == "Skin Crawl"


@respx.mock
def test_the_confirmation_is_remembered_for_every_future_run(harness: AppHarness) -> None:
    """Stored rows are only half of it: a week fetched next month has never been seen by
    this correction, and re-deriving the same wrong guess would undo the work."""
    report_id = "report-20260816-000000-wm08"
    harness.activate()
    _mismatched_report(harness, report_id)

    _correct_the_match(harness, report_id)

    remembered = harness.client.app.state.corrections.get("toy story 5")
    assert remembered is not None
    assert (remembered.tmdb_id, remembered.title, remembered.year) == (301, "Toy Story 4", 2019)
    assert remembered.imdb_url == "https://www.imdb.com/title/tt301/"


@respx.mock
def test_a_correction_is_filed_under_the_chart_title_the_row_actually_has(
    harness: AppHarness,
) -> None:
    """Read from the stored row, never from the form: the key is what every future run
    will match against, so a crafted post must not be able to choose it."""
    report_id = "report-20260816-000000-wm09"
    harness.activate()
    _mismatched_report(harness, report_id)

    _correct_the_match(harness, report_id)

    stored = harness.client.app.state.corrections.all()
    assert set(stored) == {"toy story 5"}


@respx.mock
def test_correcting_a_rank_that_is_not_there_changes_nothing(harness: AppHarness) -> None:
    report_id = "report-20260816-000000-wm10"
    respx.get(f"{FIX_API}/movie/lookup").mock(return_value=httpx.Response(200, json=CORRECTED))
    harness.activate()
    _mismatched_report(harness, report_id)

    response = harness.client.post(
        "/fix-match",
        data={"report_id": report_id, "rank": "99", "term": "Toy Story", "tmdb_id": "301"},
        follow_redirects=False,
    )

    assert harness.client.app.state.corrections.all() == {}
    assert harness.client.app.state.reports.get(report_id).movies[0].tmdb_id == 999
    assert "/reports" in response.headers["location"]
