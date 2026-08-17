"""Step 14 test: full reconcile matrix, idempotence, and failure handling."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.core import crypto
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services.apps import AppNotFoundError, AppsStore
from app.services.boxoffice import (
    MAX_RELEASE_LOOKUPS_PER_RUN,
    BoxOfficeEntry,
    ScrapeError,
)
from app.services.corrections import Correction, CorrectionStore
from app.services.filters import FiltersConfig, FiltersStore
from app.services.ignore import IgnoreStore
from app.services.pipeline import CARD_GENRES, Pipeline
from app.services.radarr import RadarrLookupResult, RadarrMovie
from app.services.release_ids import RELEASE_IDS_FILENAME, ReleaseIdCache
from app.services.reports import (
    MATCHED_BY_GUESS,
    MAX_REPORTS,
    MovieAction,
    MovieStatus,
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from tests.conftest import AppHarness

APP_ID = "app-primary"
# The one test below that drives the REAL Radarr client rather than FakeRadarr — a
# malformed HTTP response is only reachable through the client that parses it.
RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — the suite's dummy key


def _entry(rank: int, title: str) -> BoxOfficeEntry:
    return BoxOfficeEntry(rank=rank, title=title, gross_amount=rank * 1_000_000, weeks_in_release=1)


def _lookup(
    tmdb: int, title: str, year: int, genres: tuple[str, ...], rating: float
) -> RadarrLookupResult:
    return RadarrLookupResult(
        tmdb_id=tmdb, title=title, year=year, overview="x",
        poster_url=f"http://poster/{tmdb}.jpg", genres=genres, imdb_id=f"tt{tmdb}", rating=rating,
    )


class FakeRadarr:
    def __init__(self, library: list[RadarrMovie], lookup_map: dict[str, list[RadarrLookupResult]]):
        self.library = library
        self.lookup_map = lookup_map
        self.added: list[dict] = []

    async def list_movies(self) -> list[RadarrMovie]:
        return self.library

    async def lookup(self, term: str) -> list[RadarrLookupResult]:
        return self.lookup_map.get(term, [])

    async def add_movie(self, **kwargs: object) -> RadarrMovie:
        self.added.append(kwargs)
        return RadarrMovie(
            tmdb_id=kwargs["tmdb_id"], title=kwargs["title"], year=kwargs["year"], has_file=False
        )


@pytest.fixture
def env(tmp_path: Path):
    key_file = tmp_path / "k.key"
    crypto._main(["genkey", str(key_file)])
    settings = Settings(
        _env_file=None, session_secret="s" * 40, encryption_key_file=key_file,
        data_dir=tmp_path / "data",
    )
    settings.ensure_data_dirs()
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    return settings, audit


def _build_pipeline(env, radarr: FakeRadarr, config: FiltersConfig, entries,
                    ignore_store=None, **extra):
    settings, audit = env
    apps = AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit)
    filters_store = FiltersStore(settings.config_dir, audit=audit)
    filters_store.save(config)
    reports_store = ReportsStore(settings.history_dir)
    ignore_store = ignore_store or IgnoreStore(settings.config_dir, audit=audit)

    async def fetch_chart(week=None):
        return (week or "current", entries)

    pipeline = Pipeline(
        apps=apps, reports_store=reports_store,
        settings=settings, audit=audit, ignore_store=ignore_store,
        fetch_chart=fetch_chart,
        make_radarr=lambda _app_id: radarr,
        select_app_id=lambda: APP_ID,
        **extra,
    )
    return pipeline, reports_store


async def test_full_reconcile_matrix(env) -> None:
    library = [
        RadarrMovie(tmdb_id=693134, title="Dune: Part Two", year=2024, has_file=True),
        RadarrMovie(tmdb_id=872585, title="Oppenheimer", year=2023, has_file=False),
    ]
    lookup_map = {
        "Dune: Part Two": [_lookup(693134, "Dune: Part Two", 2024, ("Sci-Fi",), 8.4)],
        "Oppenheimer": [_lookup(872585, "Oppenheimer", 2023, ("Drama",), 8.1)],
        "Neon Rain": [_lookup(555, "Neon Rain", 2025, ("Action", "Sci-Fi"), 7.2)],
        "Skin Crawl": [_lookup(666, "Skin Crawl", 2025, ("Horror",), 6.0)],
        "Obscure Doc": [],  # Radarr finds nothing
    }
    radarr = FakeRadarr(library, lookup_map)
    config = FiltersConfig(quality_profile_id=4, default_root_folder="/movies")
    entries = [
        _entry(1, "Dune: Part Two"),
        _entry(2, "Oppenheimer"),
        _entry(3, "Neon Rain"),
        _entry(4, "Skin Crawl"),
        _entry(5, "Obscure Doc"),
    ]
    pipeline, reports_store = _build_pipeline(env, radarr, config, entries)

    report = await pipeline.run(trigger=RunTrigger.MANUAL)
    by_title = {movie.title: movie for movie in report.movies}

    assert report.status == RunStatus.OK
    assert report.totals.movies == 5
    assert by_title["Dune: Part Two"].status == MovieStatus.IN_LIBRARY
    assert by_title["Oppenheimer"].status == MovieStatus.WANTED
    # Review model: a missing title is reported as MISSING, never auto-added.
    assert by_title["Neon Rain"].status == MovieStatus.MISSING
    assert by_title["Neon Rain"].action == MovieAction.NONE
    assert by_title["Skin Crawl"].status == MovieStatus.MISSING
    assert by_title["Obscure Doc"].action == MovieAction.NO_MATCH
    # matched = in_library + wanted (Dune, Oppenheimer)
    assert report.totals.matched == 2
    # The run adds NOTHING to Radarr — adding is a manual, per-title action.
    assert radarr.added == []
    assert reports_store.get(report.id).totals.matched == 2
    assert by_title["Neon Rain"].imdb_url == "https://www.imdb.com/title/tt555/"


async def test_run_reports_no_app_when_connection_removed_mid_run(env) -> None:
    # The connection is deleted during the chart fetch: _make_radarr then raises
    # AppNotFoundError (a KeyError). The run must record a NO_APP report, not crash.
    settings, audit = env
    apps = AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit)
    filters_store = FiltersStore(settings.config_dir, audit=audit)
    filters_store.save(FiltersConfig())
    reports_store = ReportsStore(settings.history_dir)

    async def fetch_chart(week=None):
        return (week or "current", [_entry(1, "Neon Rain")])

    def _make_radarr_removed(_app_id):
        raise AppNotFoundError("app-gone")

    pipeline = Pipeline(
        apps=apps, reports_store=reports_store,
        settings=settings, audit=audit,
        ignore_store=IgnoreStore(settings.config_dir, audit=audit),
        fetch_chart=fetch_chart, make_radarr=_make_radarr_removed,
        select_app_id=lambda: "app-gone",
    )

    report = await pipeline.run(trigger=RunTrigger.MANUAL)
    assert report.status == RunStatus.NO_APP
    assert "removed while the run was in progress" in report.error


async def test_run_never_adds_to_radarr(env) -> None:
    lookup_map = {"Neon Rain": [_lookup(555, "Neon Rain", 2025, ("Action",), 7.0)]}
    radarr = FakeRadarr([], lookup_map)
    config = FiltersConfig(quality_profile_id=4, default_root_folder="/movies")
    entries = [_entry(1, "Neon Rain")]
    pipeline, _ = _build_pipeline(env, radarr, config, entries)

    # A run only reports — the missing title stays MISSING and nothing is added,
    # no matter how many times it runs.
    for _ in range(2):
        report = await pipeline.run(trigger=RunTrigger.SCHEDULED)
        assert report.movies[0].status == MovieStatus.MISSING
        assert report.movies[0].action == MovieAction.NONE
    assert radarr.added == []


async def test_run_records_requested_week(env) -> None:
    lookup_map = {"Neon Rain": [_lookup(555, "Neon Rain", 2025, ("Action",), 7.0)]}
    radarr = FakeRadarr([], lookup_map)
    entries = [_entry(1, "Neon Rain")]
    seen_weeks: list[str | None] = []

    settings, audit = env
    apps = AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit)
    reports_store = ReportsStore(settings.history_dir)

    async def fetch_chart(week=None):
        seen_weeks.append(week)
        return (week or "current", entries)

    pipeline = Pipeline(
        apps=apps,
        reports_store=reports_store, settings=settings, audit=audit,
        ignore_store=IgnoreStore(settings.config_dir, audit=audit),
        fetch_chart=fetch_chart, make_radarr=lambda _a: radarr, select_app_id=lambda: APP_ID,
    )

    report = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    assert report.week == "2026W02"
    assert seen_weeks == ["2026W02"]  # the week was threaded to the scraper

    current = await pipeline.run(trigger=RunTrigger.MANUAL)
    assert current.week == "current"
    assert seen_weeks[-1] is None


async def test_ignored_movie_is_never_added(env) -> None:
    settings, audit = env
    lookup_map = {"Neon Rain": [_lookup(555, "Neon Rain", 2025, ("Action",), 7.0)]}
    radarr = FakeRadarr([], lookup_map)
    config = FiltersConfig(quality_profile_id=4, default_root_folder="/movies")
    entries = [_entry(1, "Neon Rain")]

    ignore = IgnoreStore(settings.config_dir, audit=audit)
    ignore.add(tmdb_id=555, title="Neon Rain", normalized_title="neon rain")
    pipeline, _ = _build_pipeline(env, radarr, config, entries, ignore_store=ignore)

    # Even across two weekly runs, an ignored movie is never pushed to Radarr.
    for _ in range(2):
        report = await pipeline.run(trigger=RunTrigger.SCHEDULED)
        assert report.movies[0].action == MovieAction.IGNORED
    assert radarr.added == []


async def test_scrape_failure_produces_failed_report(env) -> None:
    async def failing_fetch(week=None):
        raise ScrapeError("layout changed")

    settings, audit = env
    apps = AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit)
    reports_store = ReportsStore(settings.history_dir)
    pipeline = Pipeline(
        apps=apps,
        reports_store=reports_store, settings=settings, audit=audit,
        ignore_store=IgnoreStore(settings.config_dir, audit=audit),
        fetch_chart=failing_fetch, make_radarr=lambda _a: FakeRadarr([], {}),
        select_app_id=lambda: APP_ID,
    )
    report = await pipeline.run(trigger=RunTrigger.SCHEDULED)
    assert report.status == RunStatus.SCRAPE_FAILED
    assert "layout changed" in report.error


async def test_no_app_configured_produces_failed_report(env) -> None:
    settings, audit = env
    apps = AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit)
    pipeline = Pipeline(
        apps=apps,
        reports_store=ReportsStore(settings.history_dir), settings=settings, audit=audit,
        ignore_store=IgnoreStore(settings.config_dir, audit=audit),
        fetch_chart=lambda week=None: _noop(), make_radarr=lambda _a: FakeRadarr([], {}),
        select_app_id=lambda: None,
    )
    report = await pipeline.run(trigger=RunTrigger.MANUAL)
    assert report.status == RunStatus.NO_APP


async def _noop() -> list[BoxOfficeEntry]:
    return []


@respx.mock
async def test_a_proxy_login_page_from_radarr_records_a_failed_run(harness: AppHarness) -> None:
    """The unattended run must record WHY it failed, not die.

    A 200 with HTML used to raise JSONDecodeError straight out of the scheduler job:
    no report, no audit entry, no banner — the app silently stopped working.
    """
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    respx.get("https://www.boxofficemojo.com/weekly/").mock(
        return_value=httpx.Response(
            200, text=Path("tests/fixtures/bom_weekly.html").read_text(encoding="utf-8")
        )
    )
    respx.get(f"{RADARR_URL}/api/v3/movie").mock(
        return_value=httpx.Response(200, text="<html><body>Sign in to continue</body></html>")
    )

    report = await harness.client.app.state.pipeline.run(trigger=RunTrigger.MANUAL)

    assert report.status == RunStatus.RADARR_FAILED
    assert "Could not read the Radarr library" in report.error
    assert harness.client.app.state.reports.get(report.id).id == report.id  # persisted
    assert "pipeline_run" in "\n".join(harness.audit_lines())


async def test_the_running_total_survives_the_trip_into_the_stored_report(env) -> None:
    """Scraped -> reconciled -> written to disk. Nothing else in the suite covers this
    hand-off: the month leaderboard builds its records directly, so dropping the field
    here would leave every card without a total and no test would notice."""
    entry = BoxOfficeEntry(
        rank=1, title="Long Runner", gross_amount=6_000_000,
        weeks_in_release=9, total_gross=473_000_000,
    )
    radarr = FakeRadarr([], {"Long Runner": [_lookup(555, "Long Runner", 2026, ("Drama",), 7.0)]})
    pipeline, reports_store = _build_pipeline(
        env, radarr, FiltersConfig(quality_profile_id=4, default_root_folder="/movies"), [entry]
    )

    report = await pipeline.run(trigger=RunTrigger.MANUAL)
    reloaded = reports_store.get(report.id)

    assert reloaded.movies[0].gross_amount == 6_000_000    # the week's take, unchanged
    assert reloaded.movies[0].total_gross == 473_000_000


async def test_a_chart_without_totals_still_produces_a_report(env) -> None:
    # Mojo's layout is not a contract; a missing optional column must not fail a run.
    radarr = FakeRadarr([], {"Nameless": []})
    pipeline, reports_store = _build_pipeline(
        env, radarr, FiltersConfig(quality_profile_id=4, default_root_folder="/movies"),
        [_entry(1, "Nameless")],
    )

    report = await pipeline.run(trigger=RunTrigger.MANUAL)

    assert report.status == RunStatus.OK
    assert reports_store.get(report.id).movies[0].total_gross is None


# --- one report per week: whoever fetched it first keeps it ---


class CountingRadarr(FakeRadarr):
    """A FakeRadarr that says how often the library was actually read."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.library_reads = 0

    async def list_movies(self) -> list[RadarrMovie]:
        self.library_reads += 1
        return await super().list_movies()


def _config() -> FiltersConfig:
    return FiltersConfig(quality_profile_id=4, default_root_folder="/movies")


def _chart(*, gross: int = 5_000_000, total: int | None = None) -> list[BoxOfficeEntry]:
    return [
        BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=gross,
                       weeks_in_release=1, total_gross=total),
        BoxOfficeEntry(rank=2, title="Skin Crawl", gross_amount=1_000_000,
                       weeks_in_release=3),
    ]


def _radarr() -> CountingRadarr:
    return CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})


async def test_the_scheduler_does_not_duplicate_a_week_the_admin_fetched(env) -> None:
    """The reported case: a week fetched by hand, then the weekly check comes round to
    the same chart. The second run has nothing to record."""
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    manual = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    scheduled = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert scheduled.id == manual.id            # the same report, not a second one
    assert scheduled.trigger == RunTrigger.MANUAL  # history is not rewritten either
    assert len(reports_store.list_reports()) == 1


async def test_a_manual_fetch_does_not_duplicate_a_week_the_scheduler_ran(env) -> None:
    """And the other way round, which is the half a naive "only skip scheduled runs"
    fix would miss."""
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    scheduled = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
    manual = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert manual.id == scheduled.id
    assert len(reports_store.list_reports()) == 1


async def test_an_unchanged_week_never_touches_radarr(env) -> None:
    """The skip happens before the library read, so the weekly check of a settled week
    costs one page fetch and nothing else."""
    radarr = _radarr()
    pipeline, _ = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    assert radarr.library_reads == 1

    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
    assert radarr.library_reads == 1  # not read again


async def test_a_revised_chart_updates_the_week_in_place(env) -> None:
    """Mojo publishes estimates and finalises them days later, so the same week really
    does change — but it is still the same week. The report is refreshed, not duplicated,
    and it keeps its id so every link to it survives."""
    radarr = _radarr()
    estimates = _chart(gross=5_000_000)
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), estimates)

    first = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
    # The scraper now returns the finalised figure for the same week.
    estimates[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=5_432_100,
                                  weeks_in_release=1)
    second = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert second.id == first.id                      # same card, refreshed
    assert len(reports_store.list_reports()) == 1
    assert reports_store.latest_for_week("2026W02").movies[0].gross_amount == 5_432_100
    assert second.trigger == RunTrigger.MANUAL        # and it says who last fetched it


async def test_a_weeks_worth_of_cadence_checks_leaves_one_report(env) -> None:
    """M4: what makes four checks a week affordable.

    The cadence exists because Mojo settles a week over the days after it ends. Four fires
    against a chart that has not moved must leave exactly one report and one library read
    — the extra checks cost a page fetch each and nothing more. The revision on the fourth
    is the reason they happen at all: it lands in the same report rather than a new one.
    """
    radarr = _radarr()
    estimates = _chart(gross=5_000_000)
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), estimates)

    fires = [await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
             for _ in range(3)]

    assert len({report.id for report in fires}) == 1
    assert len(reports_store.list_reports()) == 1
    assert radarr.library_reads == 1  # three fires, one read

    estimates[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=5_432_100,
                                  weeks_in_release=1)
    settled = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert settled.id == fires[0].id
    assert len(reports_store.list_reports()) == 1
    assert reports_store.latest_for_week("2026W02").movies[0].gross_amount == 5_432_100


async def test_a_newly_available_total_gross_counts_as_a_change(env) -> None:
    # Reports written before the column was read carry no total; the first run after
    # that upgrade should record the fuller chart rather than treat it as a duplicate.
    radarr = _radarr()
    entries = _chart(total=None)
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), entries)

    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
    entries[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=5_000_000,
                                weeks_in_release=1, total_gross=90_000_000)
    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert len(reports_store.list_reports()) == 1
    assert reports_store.latest_for_week("2026W02").movies[0].total_gross == 90_000_000


async def test_a_reordered_chart_is_a_change(env) -> None:
    # Same films, same money, different places: that is a different chart.
    radarr = _radarr()
    entries = _chart()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), entries)

    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")
    entries[0], entries[1] = entries[1], entries[0]
    entries[0] = BoxOfficeEntry(rank=1, title="Skin Crawl", gross_amount=1_000_000,
                                weeks_in_release=3)
    entries[1] = BoxOfficeEntry(rank=2, title="Neon Rain", gross_amount=5_000_000,
                                weeks_in_release=1)
    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert len(reports_store.list_reports()) == 1
    assert reports_store.latest_for_week("2026W02").movies[0].title == "Skin Crawl"


async def test_a_different_week_is_never_treated_as_a_duplicate(env) -> None:
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W03")

    assert len(reports_store.list_reports()) == 2


async def test_a_week_whose_last_attempt_failed_is_still_recorded(env) -> None:
    """A failed run has no chart to compare against, and must not stop the next attempt
    from writing one."""
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    reports_store.save(Report(
        id="report-2026W02-failed", run_at="2026-01-09T10:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED, week="2026W02",
        totals=ReportTotals(movies=0, matched=0), error="layout changed",
    ))

    report = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert report.status == RunStatus.OK
    assert len(reports_store.list_reports()) == 2


async def test_the_skipped_run_is_still_audited(env) -> None:
    """Otherwise there is no evidence the weekly check happened at all — the exact blind
    spot that hid a scheduler which had never once fired."""
    settings, audit = env
    radarr = _radarr()
    pipeline, _ = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    lines = (settings.logs_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"unchanged": true' in line and '"scheduled"' in line for line in lines)


async def test_a_run_collapses_duplicates_left_by_earlier_versions(env) -> None:
    """The live case: a history that already holds one week three times, from before a
    week was one thing. Reusing the report id alone would refresh one of them and leave
    the others sitting there as extra cards — the next run has to clean up too.
    """
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    for suffix, run_at in (("a", "2026-01-09T10:00:00+00:00"),
                           ("b", "2026-01-10T10:00:00+00:00"),
                           ("c", "2026-01-11T10:00:00+00:00")):
        reports_store.save(Report(
            id=f"report-2026W02-{suffix}", run_at=run_at, trigger=RunTrigger.MANUAL,
            status=RunStatus.OK, week="2026W02",
            totals=ReportTotals(movies=0, matched=0),
        ))
    assert len(reports_store.list_reports()) == 3

    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    remaining = reports_store.list_reports()
    assert len(remaining) == 1
    assert remaining[0].week == "2026W02"


# --- retention is what Settings says, applied by the run ---


async def test_a_run_prunes_to_the_configured_retention(env) -> None:
    """The setting reaches the code that deletes. Read per run, like the chart depth, so
    changing it in Settings applies to the next run without a restart."""
    settings, audit = env
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    pipeline._report_keep = lambda: 12  # noqa: SLF001 — the wired callable
    for index in range(20):
        reports_store.save(Report(
            id=f"report-old-{index:03d}", run_at=f"2026-01-01T00:00:{index:02d}+00:00",
            trigger=RunTrigger.SCHEDULED, status=RunStatus.OK, week=f"2025W{index + 1:02d}",
            totals=ReportTotals(movies=0, matched=0),
        ))

    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert len(reports_store.list_reports()) == 12


async def test_a_failed_run_prunes_to_the_same_number(env) -> None:
    """The failure path prunes too, and used to hardcode the same default the success
    path did — both read the setting now."""
    radarr = _radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    pipeline._report_keep = lambda: 5  # noqa: SLF001
    pipeline._select_app_id = lambda: None  # noqa: SLF001 — forces the NO_APP path
    for index in range(9):
        reports_store.save(Report(
            id=f"report-old-{index:03d}", run_at=f"2026-01-01T00:00:{index:02d}+00:00",
            trigger=RunTrigger.SCHEDULED, status=RunStatus.OK, week=f"2025W{index + 1:02d}",
            totals=ReportTotals(movies=0, matched=0),
        ))

    await pipeline.run(trigger=RunTrigger.SCHEDULED)

    assert len(reports_store.list_reports()) == 5


async def test_the_default_retention_is_the_stores_own(env) -> None:
    # A pipeline built without the callable behaves exactly as before this setting.
    radarr = _radarr()
    pipeline, _ = _build_pipeline(env, radarr, _config(), _chart())

    assert pipeline._report_keep() == MAX_REPORTS  # noqa: SLF001


# --- what the lookup already knew, kept instead of thrown away ---


def _rated_radarr(rating: float, genres: tuple[str, ...]) -> CountingRadarr:
    return CountingRadarr([], {
        "Neon Rain": [_lookup(555, "Neon Rain", 2026, genres, rating)],
        "Skin Crawl": [],
    })


async def test_a_report_keeps_the_rating_and_genres_the_lookup_returned(env) -> None:
    """Every run already fetched these and dropped them, leaving the weekly card to be
    decided on rank and gross alone."""
    radarr = _rated_radarr(7.2, ("Horror", "Thriller"))
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.rating == 7.2
    assert stored.genres == ["Horror", "Thriller"]


async def test_only_the_first_few_genres_are_kept(env) -> None:
    """A display bound, not a data one: the card is 208px wide and each name has to
    survive a longer translation."""
    radarr = _rated_radarr(6.0, ("Action", "Sci-Fi", "Adventure", "Comedy", "Drama"))
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert reports_store.latest_for_week("2026W02").movies[0].genres == [
        "Action", "Sci-Fi", "Adventure",
    ]
    assert CARD_GENRES == 3


async def test_a_rating_that_drifts_does_not_rewrite_the_week(env) -> None:
    """The trap. Ratings come from Radarr and move on their own; folding them into the
    fingerprint would make an unchanged chart look changed every time a score shifted a
    tenth, and rewrite a report for nothing."""
    radarr = _rated_radarr(7.2, ("Horror",))
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    written_at = first.run_at

    # Same chart, a nudged score — what happens between two ordinary weekly checks.
    radarr.lookup_map["Neon Rain"] = [_lookup(555, "Neon Rain", 2026, ("Horror",), 7.3)]
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.id == first.id
    assert again.run_at == written_at, "the week was rewritten for a rating change"
    assert len(reports_store.list_reports()) == 1
    assert reports_store.latest_for_week("2026W02").movies[0].rating == 7.2  # first answer stands


async def test_a_film_radarr_cannot_rate_still_stores(env) -> None:
    # Radarr reports no rating for plenty of titles; that is not a reason to lose the row.
    radarr = _rated_radarr(0.0, ())
    radarr.lookup_map["Neon Rain"] = [RadarrLookupResult(
        tmdb_id=555, title="Neon Rain", year=2026, overview=None,
        poster_url=None, genres=(), imdb_id=None, rating=None,
    )]
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.rating is None
    assert stored.genres == []


# --- a guess is recorded as a guess ---


def _guessing_radarr() -> CountingRadarr:
    """Radarr answers, but nothing it returns is the film — the fallback path.

    This is not a contrived case: a chart title Mojo spells its own way ("Neon Rain")
    against a lookup that only knows a sequel is exactly how the wrong poster ends up on
    a card, and why the fix-match flow exists at all.
    """
    return CountingRadarr([], {
        "Neon Rain": [_lookup(777, "Neon Rain 2", 2026, ("Action",), 6.1)],
        "Skin Crawl": [],
    })


async def test_a_fallback_match_records_that_it_guessed(env) -> None:
    pipeline, reports_store = _build_pipeline(env, _guessing_radarr(), _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.tmdb_id == 777, "the run still takes the suggestion — it just admits it"
    assert stored.detail == MATCHED_BY_GUESS


async def test_an_exact_match_records_nothing(env) -> None:
    radarr = CountingRadarr([], {
        "Neon Rain": [_lookup(555, "Neon Rain", 2026, ("Action",), 7.0)],
        "Skin Crawl": [],
    })
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert reports_store.latest_for_week("2026W02").movies[0].detail is None


async def test_a_guessed_film_already_in_the_library_still_says_so(env) -> None:
    """How it was identified is a fact about the match, not about the library. Storing it
    only while missing would drop the marker the moment someone added the film."""
    library = [RadarrMovie(tmdb_id=777, title="Neon Rain 2", year=2026, has_file=True)]
    radarr = CountingRadarr(library, {
        "Neon Rain": [_lookup(777, "Neon Rain 2", 2026, ("Action",), 6.1)],
        "Skin Crawl": [],
    })
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.status == MovieStatus.IN_LIBRARY
    assert stored.detail == MATCHED_BY_GUESS


async def test_a_match_becoming_exact_does_not_rewrite_the_week(env) -> None:
    """The same trap the rating drift test guards. `detail` is deliberately outside the
    fingerprint: Radarr's lookup index improves on its own, and a week whose chart never
    changed must not be rewritten because a guess turned into a hit."""
    radarr = _guessing_radarr()
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    radarr.lookup_map["Neon Rain"] = [_lookup(555, "Neon Rain", 2026, ("Action",), 7.0)]
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.id == first.id
    assert again.run_at == first.run_at, "the week was rewritten for a match-quality change"
    assert reports_store.latest_for_week("2026W02").movies[0].detail == MATCHED_BY_GUESS


# --- screens and the week-over-week move, stored and fingerprinted (M3) ---


def _fetching(entries: list[BoxOfficeEntry]):  # noqa: ANN202
    """Swap what the next run scrapes, leaving the rest of the pipeline alone."""
    async def fetch_chart(week=None):  # noqa: ANN001, ANN202
        return (week or "current", entries)

    return fetch_chart


def _chart_with(theaters: int | None, change: int | None) -> list[BoxOfficeEntry]:
    return [
        BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=5_000_000,
                       weeks_in_release=2, theaters=theaters, gross_change_pct=change),
        BoxOfficeEntry(rank=2, title="Skin Crawl", gross_amount=1_000_000,
                       weeks_in_release=3),
    ]


async def test_a_report_keeps_the_screen_count_and_the_move(env) -> None:
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(
        env, radarr, _config(), _chart_with(4071, -38)
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.theaters == 4071
    assert stored.gross_change_pct == -38


async def test_a_chart_without_the_columns_stores_neither(env) -> None:
    """Every report written before these existed, and every layout that drops them."""
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(
        env, radarr, _config(), _chart_with(None, None)
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.theaters is None
    assert stored.gross_change_pct is None


async def test_a_revised_screen_count_rewrites_the_week(env) -> None:
    """Mojo firms its screen counts up from estimates exactly as it does the grosses, so a
    week whose counts changed has genuinely changed and must not be treated as a no-op."""
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(
        env, radarr, _config(), _chart_with(4071, -38)
    )
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    pipeline._fetch_chart = _fetching(_chart_with(4102, -38))
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.run_at != first.run_at, "a revised screen count was treated as unchanged"
    assert reports_store.latest_for_week("2026W02").movies[0].theaters == 4102
    assert len(reports_store.list_reports()) == 1  # the week is replaced, not duplicated


async def test_a_changed_percentage_alone_does_not_rewrite_the_week(env) -> None:
    """The opposite reasoning to the line above. Mojo DERIVES this from the gross figures
    the fingerprint already carries, so a chart whose grosses are identical but whose
    percentage moved is the same chart — folding it in would report one change twice and
    rewrite a report for nothing.
    """
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(
        env, radarr, _config(), _chart_with(4071, -38)
    )
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    pipeline._fetch_chart = _fetching(_chart_with(4071, -12))
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.id == first.id
    assert again.run_at == first.run_at, "the week was rewritten for a derived figure"
    assert reports_store.latest_for_week("2026W02").movies[0].gross_change_pct == -38


# --- a run records which chart it came from, and in whose money (M1) ---


def _pounds_chart() -> list[BoxOfficeEntry]:
    return [
        BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=4_209_000,
                       weeks_in_release=1, currency_symbol="£"),
        BoxOfficeEntry(rank=2, title="Skin Crawl", gross_amount=1_100_000,
                       weeks_in_release=2, currency_symbol="£"),
    ]


async def test_a_regional_run_stores_its_region_and_its_currency(env) -> None:
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _pounds_chart())
    pipeline._region = lambda: "GB"

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02")
    assert stored.region == "GB"
    assert stored.currency == "£"
    assert stored.movies[0].gross_display == "£4.2M"


async def test_a_domestic_run_is_unchanged(env) -> None:
    """Every existing install is on this path and none of it may move."""
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02")
    assert stored.region == ""
    assert stored.currency == "$"
    assert stored.movies[0].gross_display.startswith("$")


async def test_a_report_from_before_regions_existed_reads_as_domestic_dollars(
    env,
) -> None:
    """Additive fields with defaults, so REPORT_SCHEMA_VERSION does not move and an old
    file reads back as exactly what it was."""
    settings, _ = env
    store = ReportsStore(settings.history_dir)
    path = settings.history_dir / "report-old.json"
    path.write_text(
        '{"schema_version": 1, "id": "report-old", "run_at": "2026-01-01T00:00:00+00:00",'
        ' "trigger": "manual", "status": "ok", "week": "2026W01",'
        ' "totals": {"movies": 0, "matched": 0}, "movies": []}',
        encoding="utf-8",
    )

    stored = store.latest_for_week("2026W01")

    assert stored.region == ""
    assert stored.currency == "$"


async def test_re_running_a_week_under_a_new_region_replaces_it(env) -> None:
    """The acceptance criterion. A stored week fetched from a different chart is not this
    chart, whatever its numbers happen to be — so the dedupe must not treat it as a no-op.
    """
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    assert first.region == ""

    pipeline._region = lambda: "GB"
    pipeline._fetch_chart = _fetching(_pounds_chart())
    again = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert again.id == first.id  # the week keeps its id, so links survive
    assert again.run_at != first.run_at
    stored = reports_store.latest_for_week("2026W02")
    assert (stored.region, stored.currency) == ("GB", "£")
    assert len(reports_store.list_reports()) == 1  # replaced, not duplicated


async def test_the_same_chart_in_the_same_region_is_still_a_no_op(env) -> None:
    """The region check must not defeat the dedupe it sits in front of."""
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _pounds_chart())
    pipeline._region = lambda: "GB"
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.run_at == first.run_at
    assert len(reports_store.list_reports()) == 1


async def test_the_configured_region_reaches_the_scrape(env) -> None:
    """Read per run, like the depth: changing it in Settings applies to the next run
    without a restart."""
    seen: dict[str, object] = {}

    async def _capture(*, snapshot_dir, url, week, top_n, area):  # noqa: ANN001, ANN202
        seen["area"] = area
        raise ScrapeError("stop here — the region is what's under test")

    radarr = CountingRadarr([], {})
    pipeline, _ = _build_pipeline(env, radarr, _config(), _chart())
    pipeline._fetch_chart = pipeline._default_fetch_chart
    pipeline._region = lambda: "DE"
    import app.services.boxoffice as boxoffice_module

    original = boxoffice_module.fetch_weekly_chart
    boxoffice_module.fetch_weekly_chart = _capture
    try:
        await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    finally:
        boxoffice_module.fetch_weekly_chart = original

    assert seen["area"] == "DE"


async def test_the_same_numbers_from_a_different_chart_are_not_the_same_week(env) -> None:
    """The region guard on its own, with the fingerprint deliberately held identical.

    The re-run test above changes the numbers too, so the fingerprint alone would catch
    it — and a guard that is never the reason a test passes is a guard nobody would miss
    if it went. Two charts can print the same figures; they are still two charts.
    """
    radarr = CountingRadarr([], {"Neon Rain": [], "Skin Crawl": []})
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), _chart())
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    pipeline._region = lambda: "GB"  # same chart contents, different chart
    again = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert again.run_at != first.run_at, "a different region was treated as unchanged"
    assert reports_store.latest_for_week("2026W02").region == "GB"
    assert len(reports_store.list_reports()) == 1


# --- a guess checks the IMDb id before guessing (M5) ---


def _linked_chart(*paths: str | None) -> list[BoxOfficeEntry]:
    """A chart whose rows link the release pages Mojo gives them."""
    return [
        BoxOfficeEntry(rank=index + 1, title=title, gross_amount=(index + 1) * 1_000_000,
                       weeks_in_release=1, release_path=path)
        for index, (title, path) in enumerate(
            zip(("Neon Rain", "Skin Crawl"), paths, strict=False)
        )
    ]


class _CountingReleaseFetch:
    """Answers release-page lookups, and says how many were actually made."""

    def __init__(self, answers: dict[str, str | None]) -> None:
        self.answers = answers
        self.paths: list[str] = []

    async def __call__(self, release_path: str) -> str | None:
        self.paths.append(release_path)
        return self.answers.get(release_path)


def _confirming_pipeline(env, radarr, entries, fetch: _CountingReleaseFetch):
    """The ordinary builder, plus the release-id seam and a real on-disk cache."""
    settings, _audit = env
    return _build_pipeline(
        env, radarr, _config(), entries,
        fetch_release_id=fetch, release_ids=ReleaseIdCache(settings.cache_dir),
    )


def _two_candidate_radarr() -> CountingRadarr:
    """Radarr offers the sequel first and the film itself second — the ordering that put
    the wrong poster on a card, and the reason the fix-match flow exists."""
    return CountingRadarr([], {
        "Neon Rain": [
            _lookup(777, "Neon Rain 2", 2026, ("Action",), 6.1),
            _lookup(555, "Neon Rain: Redux", 2026, ("Action",), 7.4),
        ],
        "Skin Crawl": [],
    })


async def test_the_release_page_picks_the_candidate_the_title_could_not(env) -> None:
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart("/release/rl1/", None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.tmdb_id == 555, "the first suggestion was taken over the confirmed id"
    assert stored.detail is None, "a confirmed match is not a guess"


async def test_a_confirmed_match_carries_the_confirmed_films_links(env) -> None:
    """The card's poster, rating and IMDb link all come off the chosen candidate — picking
    the right film and then showing the wrong one's artwork would be its own bug."""
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart("/release/rl1/", None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.imdb_url is not None and stored.imdb_url.endswith("tt555/")
    assert stored.poster_url == "http://poster/555.jpg"
    assert stored.rating == 7.4


async def test_an_id_that_matches_no_candidate_stays_a_guess(env) -> None:
    """Mojo knows the film and Radarr does not have it. Today's behaviour, unchanged."""
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt999999"})
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart("/release/rl1/", None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.tmdb_id == 777
    assert stored.detail == MATCHED_BY_GUESS


async def test_an_unreachable_release_page_leaves_the_guess_exactly_as_it_was(env) -> None:
    fetch = _CountingReleaseFetch({})  # the page answers nothing
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart("/release/rl1/", None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.tmdb_id == 777
    assert stored.detail == MATCHED_BY_GUESS


async def test_a_row_with_no_release_link_costs_no_request(env) -> None:
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    pipeline, _ = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart(None, None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert fetch.paths == []


async def test_an_exact_title_match_never_fetches_a_release_page(env) -> None:
    """The guess path only. A run whose titles Radarr recognises costs Mojo nothing beyond
    the chart itself, which is what makes this affordable at all."""
    radarr = CountingRadarr([], {
        "Neon Rain": [_lookup(555, "Neon Rain", 2026, ("Action",), 7.0)],
        "Skin Crawl": [],
    })
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    pipeline, reports_store = _confirming_pipeline(
        env, radarr, _linked_chart("/release/rl1/", "/release/rl2/"), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert fetch.paths == []
    assert reports_store.latest_for_week("2026W02").movies[0].detail is None


async def test_a_resolved_id_is_asked_for_once_ever(env) -> None:
    """The answer never changes — one release page is one film forever — so a film that
    stays on the chart for eight weeks costs one request, not eight."""
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    entries = _linked_chart("/release/rl1/", None)
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), entries, fetch
    )
    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    assert fetch.paths == ["/release/rl1/"]

    # A genuinely changed chart, so the run does not stop at the unchanged check.
    entries[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=9_999_999,
                                weeks_in_release=2, release_path="/release/rl1/")
    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert fetch.paths == ["/release/rl1/"], "the cache was not consulted"
    assert reports_store.latest_for_week("2026W02").movies[0].tmdb_id == 555


async def test_a_page_that_could_not_answer_is_not_remembered_as_no(env) -> None:
    """Caching a failure would pin one moment's outage to a film permanently."""
    fetch = _CountingReleaseFetch({})
    entries = _linked_chart("/release/rl1/", None)
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), entries, fetch
    )
    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    fetch.answers["/release/rl1/"] = "tt555"  # the page is back
    entries[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=9_999_999,
                                weeks_in_release=2, release_path="/release/rl1/")
    await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert len(fetch.paths) == 2, "the failure was cached"
    assert reports_store.latest_for_week("2026W02").movies[0].detail is None


async def test_the_lookup_budget_holds_for_a_whole_chart_of_guesses(env) -> None:
    """A week where Radarr recognises nothing must not become a dozen extra requests."""
    titles = [f"Unknown {index}" for index in range(MAX_RELEASE_LOOKUPS_PER_RUN + 3)]
    radarr = CountingRadarr([], {
        title: [_lookup(900 + index, f"{title} 2", 2026, (), 5.0)]
        for index, title in enumerate(titles)
    })
    entries = [
        BoxOfficeEntry(rank=index + 1, title=title, gross_amount=1_000_000,
                       weeks_in_release=1, release_path=f"/release/rl{index}/")
        for index, title in enumerate(titles)
    ]
    fetch = _CountingReleaseFetch({})
    pipeline, reports_store = _confirming_pipeline(env, radarr, entries, fetch)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    assert len(fetch.paths) == MAX_RELEASE_LOOKUPS_PER_RUN
    # Every title still recorded — a spent budget skips the confirmation, not the row.
    assert len(reports_store.latest_for_week("2026W02").movies) == len(titles)


async def test_the_budget_is_per_run_not_per_pipeline(env) -> None:
    """A backfill of twelve weeks would otherwise spend its whole budget on week one."""
    titles = [f"Unknown {index}" for index in range(MAX_RELEASE_LOOKUPS_PER_RUN)]
    radarr = CountingRadarr([], {
        title: [_lookup(900 + index, f"{title} 2", 2026, (), 5.0)]
        for index, title in enumerate(titles)
    })
    entries = [
        BoxOfficeEntry(rank=index + 1, title=title, gross_amount=1_000_000,
                       weeks_in_release=1, release_path=f"/release/rl{index}/")
        for index, title in enumerate(titles)
    ]
    fetch = _CountingReleaseFetch({})
    pipeline, _ = _confirming_pipeline(env, radarr, entries, fetch)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")
    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W03")

    assert len(fetch.paths) == 2 * MAX_RELEASE_LOOKUPS_PER_RUN


async def test_a_release_link_alone_does_not_rewrite_a_stored_week(env) -> None:
    """Mojo's routing is not chart data. A rotated release id must not make a week whose
    figures never moved look changed — the trap the rating and match-quality tests guard."""
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555", "/release/rl9/": "tt555"})
    entries = _linked_chart("/release/rl1/", None)
    pipeline, reports_store = _confirming_pipeline(
        env, _two_candidate_radarr(), entries, fetch
    )
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    entries[0] = BoxOfficeEntry(rank=1, title="Neon Rain", gross_amount=1_000_000,
                                weeks_in_release=1, release_path="/release/rl9/")
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W02")

    assert again.id == first.id
    assert again.run_at == first.run_at, "the week was rewritten for a routing change"
    assert fetch.paths == ["/release/rl1/"], "an unchanged week reached the network"


async def test_the_resolved_id_is_written_where_a_backup_will_carry_it(env) -> None:
    """Under /data/cache, beside the posters — the directory the backup already sweeps, so
    a restore does not re-fetch every release page the install ever resolved."""
    settings, _audit = env
    fetch = _CountingReleaseFetch({"/release/rl1/": "tt555"})
    pipeline, _ = _confirming_pipeline(
        env, _two_candidate_radarr(), _linked_chart("/release/rl1/", None), fetch
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    cache_file = settings.cache_dir / RELEASE_IDS_FILENAME
    assert cache_file.is_file()
    assert ReleaseIdCache(settings.cache_dir).get("/release/rl1/") == "tt555"


@respx.mock
async def test_the_real_fetcher_reaches_mojos_release_page(env) -> None:
    """Every test above injects the seam, so this is the one that proves the seam's
    default actually builds the right URL and reads the right thing off it."""
    settings, _audit = env
    respx.get("https://www.boxofficemojo.com/release/rl1/").mock(
        return_value=httpx.Response(200, text='<a href="/title/tt15239678/">IMDbPro</a>')
    )
    radarr = CountingRadarr([], {
        "Neon Rain": [
            _lookup(777, "Neon Rain 2", 2026, ("Action",), 6.1),
            RadarrLookupResult(
                tmdb_id=555, title="Neon Rain: Redux", year=2026, overview="x",
                poster_url=None, genres=(), imdb_id="tt15239678", rating=7.4,
            ),
        ],
        "Skin Crawl": [],
    })
    pipeline, reports_store = _build_pipeline(
        env, radarr, _config(), _linked_chart("/release/rl1/", None),
        release_ids=ReleaseIdCache(settings.cache_dir),
    )

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W02")

    stored = reports_store.latest_for_week("2026W02").movies[0]
    assert stored.tmdb_id == 555
    assert stored.detail is None


# --- a confirmed match outranks anything the run works out for itself ---


def _corrected(env, radarr, entries, chart_title: str, correction: Correction):
    """The ordinary builder with a correction already on file."""
    settings, _audit = env
    store = CorrectionStore(settings.config_dir)
    store.save(chart_title, correction)
    pipeline, reports_store = _build_pipeline(env, radarr, _config(), entries, corrections=store)
    return pipeline, reports_store, store


MIROIRS = [BoxOfficeEntry(rank=26, title="Miroirs No. 3", gross_amount=1_000_000,
                          weeks_in_release=3)]
MIRRORS = Correction(
    tmdb_id=111, title="Mirrors No. 3", year=2025,
    imdb_url="https://www.imdb.com/title/tt15239678/", poster_url="http://img/ok.jpg",
)


def _sequel_radarr() -> CountingRadarr:
    """Radarr only knows the sequel — the case a human had to settle by hand."""
    return CountingRadarr([], {
        "Miroirs No. 3": [_lookup(999, "Miroirs No. 3: The Sequel", 2026, ("Drama",), 5.0)],
    })


async def test_a_confirmed_match_is_used_instead_of_a_guess(env) -> None:
    pipeline, reports_store, _ = _corrected(env, _sequel_radarr(), MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    stored = reports_store.latest_for_week("2026W32").movies[0]
    assert (stored.tmdb_id, stored.title, stored.year) == (111, "Mirrors No. 3", 2025)
    assert stored.imdb_url == "https://www.imdb.com/title/tt15239678/"
    assert stored.detail is None, "a human confirmed it — that is not a guess"


async def test_a_correction_reaches_every_later_week_by_itself(env) -> None:
    """The film charts for weeks. Fixing it once must not mean fixing it every Monday."""
    pipeline, reports_store, _ = _corrected(env, _sequel_radarr(), MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")
    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W33")

    for week in ("2026W32", "2026W33"):
        assert reports_store.latest_for_week(week).movies[0].tmdb_id == 111, week


async def test_a_confirmed_match_outranks_even_an_exact_title_match(env) -> None:
    """Radarr's own titles are not authoritative — two films share a title, and the admin
    is the one who looked at both posters."""
    radarr = CountingRadarr([], {
        "Miroirs No. 3": [_lookup(999, "Miroirs No. 3", 2026, ("Drama",), 5.0)],
    })
    pipeline, reports_store, _ = _corrected(env, radarr, MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    assert reports_store.latest_for_week("2026W32").movies[0].tmdb_id == 111


async def test_a_confirmed_match_holds_when_radarr_finds_nothing_at_all(env) -> None:
    """The likeliest case for a correction to exist: Mojo's spelling finds no results, so
    a run that only consulted the lookup would go back to a dead end every week."""
    radarr = CountingRadarr([], {"Miroirs No. 3": []})
    pipeline, reports_store, _ = _corrected(env, radarr, MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    stored = reports_store.latest_for_week("2026W32").movies[0]
    assert stored.tmdb_id == 111
    assert stored.action != MovieAction.NO_MATCH


async def test_a_corrected_row_still_reads_the_library_and_the_ignore_list(env) -> None:
    """A correction says WHICH film, not what to do about it."""
    library = [RadarrMovie(tmdb_id=111, title="Mirrors No. 3", year=2025, has_file=True)]
    radarr = CountingRadarr(library, {"Miroirs No. 3": []})
    pipeline, reports_store, _ = _corrected(env, radarr, MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    assert reports_store.latest_for_week("2026W32").movies[0].status == MovieStatus.IN_LIBRARY


async def test_a_corrected_row_takes_the_live_rating_when_radarr_still_offers_it(
    env,
) -> None:
    """The one thing not frozen into the confirmation: a score drifts, and a year-old
    snapshot of one is worse than none."""
    radarr = CountingRadarr([], {
        "Miroirs No. 3": [_lookup(111, "Mirrors No. 3", 2025, ("Drama", "Thriller"), 6.5)],
    })
    pipeline, reports_store, _ = _corrected(env, radarr, MIROIRS, "miroirs no 3", MIRRORS)

    await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    stored = reports_store.latest_for_week("2026W32").movies[0]
    assert stored.rating == 6.5
    assert stored.genres == ["Drama", "Thriller"]


async def test_a_correction_does_not_make_an_unchanged_week_look_changed(env) -> None:
    """The bug that made corrections evaporate: the fix rewrote the row's chart identity,
    the week stopped matching its own fingerprint, and the next check overwrote it."""
    settings, _audit = env
    corrections = CorrectionStore(settings.config_dir)
    pipeline, reports_store = _build_pipeline(
        env, _sequel_radarr(), _config(), MIROIRS, corrections=corrections
    )
    first = await pipeline.run(trigger=RunTrigger.MANUAL, week="2026W32")

    corrections.save("miroirs no 3", MIRRORS)
    reports_store.apply_correction("miroirs no 3", MIRRORS)
    again = await pipeline.run(trigger=RunTrigger.SCHEDULED, week="2026W32")

    assert again.id == first.id
    assert again.run_at == first.run_at, "an untouched chart was rewritten"
    stored = reports_store.latest_for_week("2026W32").movies[0]
    assert stored.tmdb_id == 111, "the correction was thrown away by the next check"
    assert stored.normalized_title == "miroirs no 3", "the row left its own chart line"
