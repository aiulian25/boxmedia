"""The schedule's own record of what it did, apart from what it found."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core import filestore
from app.services import scheduler as scheduler_module
from app.services.boxoffice import (
    FETCH_FAILED_PREFIX,
    NO_DATA_PREFIX,
    ScrapeError,
    fetch_weekly_chart,
)
from app.services.filters import SCHEDULE_MODE_CADENCE, SCHEDULE_MODE_INTERVAL
from app.services.ingestion_state import (
    FAILURE_MALFORMED,
    FAILURE_TRANSIENT,
    FAILURE_UNAVAILABLE,
    FIRST_RETRY_AFTER,
    INGESTION_FILENAME,
    INGESTION_SCHEMA_VERSION,
    LONGEST_RETRY_AFTER,
    MAX_ATTEMPTS,
    OUTCOME_CHANGED,
    OUTCOME_FAILED,
    OUTCOME_UNCHANGED,
    IngestionStateStore,
    ScheduledCheck,
    WeekRetry,
    failure_class_of,
    gave_up,
    retry_is_due,
)
from app.services.reports import Report, ReportsStore, ReportTotals, RunStatus, RunTrigger
from app.services.scheduler import (
    CATCHUP_JOB_ID,
    STARTUP_GRACE_SECONDS,
    BoxMediaScheduler,
)

INTERVAL_HOURS = 168
THEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LATER = THEN + timedelta(hours=1)


def _report(run_at: datetime, *, trigger: str = RunTrigger.MANUAL, status: str = RunStatus.OK):
    return Report(
        id="report-2026W30-0000", run_at=run_at.isoformat(), trigger=trigger, status=status,
        week="2026W30", totals=ReportTotals(movies=0, matched=0),
    )


class _Pipeline:
    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer

    async def run(self, **_: object) -> Report:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _scheduler(tmp_path: Path, pipeline: object = None, **kwargs: object) -> BoxMediaScheduler:
    return BoxMediaScheduler(
        pipeline, interval_hours=INTERVAL_HOURS, reports=ReportsStore(tmp_path),
        ingestion=IngestionStateStore(tmp_path), **kwargs,
    )


# --- the record ---


def test_an_install_with_no_record_reads_as_empty(tmp_path: Path) -> None:
    assert IngestionStateStore(tmp_path).read() == ScheduledCheck()


def test_it_survives_a_reopen(tmp_path: Path) -> None:
    IngestionStateStore(tmp_path).record_attempt(THEN)
    IngestionStateStore(tmp_path).record_outcome(OUTCOME_CHANGED, LATER)

    check = IngestionStateStore(tmp_path).read()

    assert check.attempted_at == THEN
    assert check.finished_at == LATER
    assert check.outcome == OUTCOME_CHANGED


def test_each_time_answers_its_own_question(tmp_path: Path) -> None:
    """An unchanged check is a success with no change. A failure is neither."""
    store = IngestionStateStore(tmp_path)
    store.record_attempt(THEN)
    store.record_outcome(OUTCOME_CHANGED, THEN)
    store.record_attempt(LATER)
    store.record_outcome(OUTCOME_UNCHANGED, LATER)

    assert store.read().succeeded_at == LATER
    assert store.read().changed_at == THEN

    much_later = LATER + timedelta(hours=1)
    store.record_attempt(much_later)
    store.record_outcome(OUTCOME_FAILED, much_later)

    assert store.read().finished_at == much_later
    assert store.read().succeeded_at == LATER
    assert store.read().changed_at == THEN


def test_an_attempt_under_way_has_no_outcome_yet(tmp_path: Path) -> None:
    store = IngestionStateStore(tmp_path)
    store.record_attempt(THEN)
    store.record_outcome(OUTCOME_CHANGED, THEN)

    store.record_attempt(LATER)

    assert store.read().outcome is None


@pytest.mark.parametrize(
    "body",
    ["attempted_at: 7\n", "attempted_at: not-a-time\n", "outcome: exploded\n", "- a\n- list\n"],
)
def test_a_damaged_record_is_refused_strictly_and_tolerated_at_runtime(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / INGESTION_FILENAME).write_text(body, encoding="utf-8")
    store = IngestionStateStore(tmp_path)

    with pytest.raises(ValueError):  # noqa: PT011
        store.read()
    assert store.load() == ScheduledCheck()


def test_a_full_disk_does_not_stop_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(filestore, "write_yaml", full)

    IngestionStateStore(tmp_path).record_attempt(THEN)


def test_it_holds_no_chart_data(tmp_path: Path) -> None:
    IngestionStateStore(tmp_path).record_attempt(THEN)

    stored = filestore.read_yaml(
        tmp_path / INGESTION_FILENAME, expected_version=INGESTION_SCHEMA_VERSION
    )

    assert set(stored) == {
        "schema_version", "outcome", "attempted_at", "finished_at", "succeeded_at", "changed_at",
        "week_retries",
    }


# --- the check records itself ---


async def test_a_check_finding_nothing_new_still_counts_and_writes_no_chart(
    tmp_path: Path,
) -> None:
    """The audit's reproduction: a manual report, then an identical scheduled check, left
    last_scheduled=None."""
    store = ReportsStore(tmp_path)
    stored = _report(THEN)
    store.save(stored)
    chart = tmp_path / f"{stored.id}.json"
    before = chart.read_bytes()
    scheduler = _scheduler(tmp_path, _Pipeline(stored))

    await scheduler._run_scheduled()

    assert scheduler.last_run_at() > THEN
    assert scheduler.last_outcome() == OUTCOME_UNCHANGED
    assert chart.read_bytes() == before


async def test_a_check_that_stored_a_chart_says_so(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, _Pipeline(_report(datetime.now(UTC) + timedelta(seconds=1))))

    await scheduler._run_scheduled()

    assert scheduler.last_outcome() == OUTCOME_CHANGED


async def test_a_failed_run_is_a_failed_check_and_still_an_attempt(tmp_path: Path) -> None:
    scheduler = _scheduler(
        tmp_path,
        _Pipeline(
            _report(datetime.now(UTC) + timedelta(seconds=1), status=RunStatus.RADARR_FAILED)
        ),
    )

    await scheduler._run_scheduled()

    assert scheduler.last_outcome() == OUTCOME_FAILED
    assert scheduler.last_run_at() is not None


async def test_a_crash_inside_the_run_is_recorded_and_not_swallowed(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, _Pipeline(RuntimeError("bug")))

    with pytest.raises(RuntimeError):
        await scheduler._run_scheduled()

    assert scheduler.last_outcome() == OUTCOME_FAILED


async def test_a_cancelled_run_keeps_its_attempt_and_claims_no_outcome(tmp_path: Path) -> None:
    class _Hanging:
        async def run(self, **_: object) -> Report:
            await asyncio.sleep(3600)

    scheduler = _scheduler(tmp_path, _Hanging())
    task = asyncio.create_task(scheduler._run_scheduled())
    await asyncio.sleep(0)

    task.cancel()
    await asyncio.wait({task})

    assert scheduler.last_run_at() is not None
    assert scheduler.last_outcome() is None


async def test_a_stored_report_with_a_hand_edited_stamp_does_not_lose_the_gap_fill(
    tmp_path: Path,
) -> None:
    report = _report(THEN).model_copy(update={"run_at": "yesterday"})
    scheduler = _scheduler(tmp_path, _Pipeline(report))

    await scheduler._run_scheduled()

    assert scheduler.last_outcome() == OUTCOME_UNCHANGED


# --- what a restart resumes from ---


def test_a_manual_run_taking_over_the_report_does_not_erase_the_schedule(tmp_path: Path) -> None:
    """The report's trigger now says manual, and the record still knows."""
    ReportsStore(tmp_path).save(_report(datetime.now(UTC), trigger=RunTrigger.MANUAL))
    recent = datetime.now(UTC) - timedelta(hours=2)
    IngestionStateStore(tmp_path).record_attempt(recent)

    assert _scheduler(tmp_path).last_run_at() == recent


def test_a_restart_anchors_an_interval_on_the_last_attempt(tmp_path: Path) -> None:
    recent = datetime.now(UTC) - timedelta(hours=2)
    IngestionStateStore(tmp_path).record_attempt(recent)

    first = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_INTERVAL)._first_run_at()

    assert first == recent + timedelta(hours=INTERVAL_HOURS)


def test_a_restart_after_a_recent_unchanged_check_queues_no_catch_up(tmp_path: Path) -> None:
    """It re-queued on every boot, because unchanged checks left no report to find."""
    IngestionStateStore(tmp_path).record_attempt(datetime.now(UTC) - timedelta(hours=2))
    scheduler = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_CADENCE)

    scheduler._add_chart_job()

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is None


def test_a_long_silence_is_still_caught_up(tmp_path: Path) -> None:
    IngestionStateStore(tmp_path).record_attempt(datetime.now(UTC) - timedelta(days=30))
    scheduler = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_CADENCE)

    scheduler._add_chart_job()

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is not None


def test_an_install_upgrading_starts_from_its_reports(tmp_path: Path) -> None:
    moment = datetime.now(UTC) - timedelta(hours=3)
    ReportsStore(tmp_path).save(_report(moment, trigger=RunTrigger.SCHEDULED))

    assert _scheduler(tmp_path).last_run_at() == moment


def test_a_damaged_record_falls_back_to_the_reports(tmp_path: Path) -> None:
    moment = datetime.now(UTC) - timedelta(hours=3)
    ReportsStore(tmp_path).save(_report(moment, trigger=RunTrigger.SCHEDULED))
    (tmp_path / INGESTION_FILENAME).write_text("attempted_at: 7\n", encoding="utf-8")

    assert _scheduler(tmp_path).last_run_at() == moment


def test_a_fresh_install_still_runs_after_the_grace(tmp_path: Path) -> None:
    first = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_INTERVAL)._first_run_at()

    assert first - datetime.now(UTC) <= timedelta(seconds=STARTUP_GRACE_SECONDS)


# --- changing modes ---


async def test_leaving_cadence_mode_removes_its_pending_catch_up(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_CADENCE)
    scheduler.start()
    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is not None

    scheduler.reschedule(INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_INTERVAL)

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is None
    scheduler.shutdown()


async def test_returning_to_cadence_mode_can_queue_it_again(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_INTERVAL)
    scheduler.start()

    scheduler.reschedule(INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_CADENCE)

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is not None
    scheduler.shutdown()


async def test_a_mode_switch_keeps_the_anchor_for_a_recent_attempt(tmp_path: Path) -> None:
    recent = datetime.now(UTC) - timedelta(hours=2)
    IngestionStateStore(tmp_path).record_attempt(recent)
    scheduler = _scheduler(tmp_path, schedule_mode=SCHEDULE_MODE_CADENCE)
    scheduler.start()

    scheduler.reschedule(INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_INTERVAL)

    assert scheduler.next_run_at() is not None
    delay = scheduler.next_run_at() - (recent + timedelta(hours=INTERVAL_HOURS))
    assert abs(delay) < timedelta(seconds=5)
    scheduler.shutdown()


# --- retrying a failed week ---

FAILED_WEEK = "2026W31"


def _failed(status: str, error: str | None = None) -> Report:
    return Report(
        id=f"report-{FAILED_WEEK}-failed", run_at=THEN.isoformat(), trigger=RunTrigger.MANUAL,
        status=status, week=FAILED_WEEK, totals=ReportTotals(movies=0, matched=0), error=error,
    )


class _Backfill:
    def __init__(self) -> None:
        self.started: list[list[str]] = []

    def start(self, weeks: list[str]) -> bool:
        self.started.append(weeks)
        return True


def _clock_at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:  # noqa: ANN001, ARG003
            return moment

    monkeypatch.setattr(scheduler_module, "datetime", _Frozen)


def _due_weeks(tmp_path: Path) -> list[str]:
    """Through a new store and scheduler, as a restart would be."""
    return _scheduler(tmp_path)._weeks_due_a_retry()


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (RunStatus.RADARR_FAILED, "Radarr refused", FAILURE_TRANSIENT),
        (RunStatus.NO_APP, "No Radarr connection", FAILURE_TRANSIENT),
        (RunStatus.SCRAPE_FAILED, f"{FETCH_FAILED_PREFIX}: timed out", FAILURE_TRANSIENT),
        (RunStatus.SCRAPE_FAILED, f"{NO_DATA_PREFIX} for week 2026W31", FAILURE_UNAVAILABLE),
        (RunStatus.SCRAPE_FAILED, "chart ranks are not continuous from 1", FAILURE_MALFORMED),
        (RunStatus.SCRAPE_FAILED, "box-office results table not found", FAILURE_MALFORMED),
    ],
)
def test_a_failure_is_classed_from_what_it_stored(status, error, expected) -> None:  # noqa: ANN001
    assert failure_class_of(_failed(status, error)) == expected


@pytest.mark.parametrize(
    ("http_status", "expected"),
    [(404, FAILURE_UNAVAILABLE), (410, FAILURE_UNAVAILABLE),
     (503, FAILURE_TRANSIENT), (429, FAILURE_TRANSIENT)],
)
async def test_an_http_failure_is_classed_from_the_real_error_text(
    tmp_path: Path, http_status: int, expected: str,
) -> None:
    """The real wording, because the class is read from it. The week id holds "404" so a bare
    digit match would call every outage of that week a page that does not exist."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(http_status))
    )

    with pytest.raises(ScrapeError) as caught:
        await fetch_weekly_chart(snapshot_dir=tmp_path, week="2404W10", client=client)
    await client.aclose()

    assert failure_class_of(_failed(RunStatus.SCRAPE_FAILED, str(caught.value))) == expected


def test_the_gap_between_attempts_doubles_and_is_capped(tmp_path: Path) -> None:
    store = IngestionStateStore(tmp_path)
    gaps = []
    for _ in range(MAX_ATTEMPTS[FAILURE_TRANSIENT] + 2):
        store.record_week_failure(FAILED_WEEK, THEN)
        gaps.append(store.week_retries()[FAILED_WEEK].next_retry_at - THEN)

    assert gaps[0] == FIRST_RETRY_AFTER
    assert gaps[1] == FIRST_RETRY_AFTER * 2
    assert gaps[-1] == LONGEST_RETRY_AFTER
    assert gaps == sorted(gaps)


def test_recording_a_check_keeps_the_retries_and_the_other_way_round(tmp_path: Path) -> None:
    store = IngestionStateStore(tmp_path)
    store.record_attempt(THEN)
    store.record_week_failure(FAILED_WEEK, THEN)
    store.record_outcome(OUTCOME_CHANGED, LATER)

    assert store.week_retries()[FAILED_WEEK].attempts == 1
    assert store.read().changed_at == LATER


@pytest.mark.parametrize(
    "stored",
    [
        "not a mapping",
        {FAILED_WEEK: "not a mapping"},
        {FAILED_WEEK: {"attempts": "two", "next_retry_at": THEN.isoformat()}},
        {FAILED_WEEK: {"attempts": 2}},
        {FAILED_WEEK: {"attempts": 2, "next_retry_at": "soon"}},
        {FAILED_WEEK: {"attempts": 2, "next_retry_at": "2026-10-01T12:00:00"}},
    ],
    ids=["list", "entry", "attempts", "no-time", "bad-time", "naive-time"],
)
def test_a_damaged_retry_record_is_refused_strictly_and_tolerated_at_runtime(
    tmp_path: Path, stored: object,
) -> None:
    """Strict for a backup's validation, tolerant for the schedule, which must not stop."""
    store = IngestionStateStore(tmp_path)
    store.record_attempt(THEN)
    path = tmp_path / INGESTION_FILENAME
    document = filestore.read_yaml(path, expected_version=INGESTION_SCHEMA_VERSION)
    document["week_retries"] = stored
    filestore.write_yaml(path, document, schema_version=INGESTION_SCHEMA_VERSION)

    with pytest.raises(ValueError):
        store.read()
    assert store.week_retries() == {}
    assert store.load() == ScheduledCheck()


def test_a_failure_older_than_this_record_is_due_at_once(tmp_path: Path) -> None:
    """The reports an outage stranded before any of this existed have no retry record."""
    ReportsStore(tmp_path).save(_failed(RunStatus.RADARR_FAILED, "Radarr refused"))

    assert _due_weeks(tmp_path) == [FAILED_WEEK]


def test_backoff_holds_across_a_restart_until_its_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ReportsStore(tmp_path).save(_failed(RunStatus.RADARR_FAILED, "Radarr refused"))
    IngestionStateStore(tmp_path).record_week_failure(FAILED_WEEK, THEN)

    _clock_at(monkeypatch, THEN + FIRST_RETRY_AFTER - timedelta(seconds=1))
    assert _due_weeks(tmp_path) == []

    _clock_at(monkeypatch, THEN + FIRST_RETRY_AFTER)
    assert _due_weeks(tmp_path) == [FAILED_WEEK]


@pytest.mark.parametrize(
    ("status", "error", "attempts", "stopped"),
    [
        (RunStatus.RADARR_FAILED, "down", MAX_ATTEMPTS[FAILURE_TRANSIENT] - 1, False),
        (RunStatus.RADARR_FAILED, "down", MAX_ATTEMPTS[FAILURE_TRANSIENT], True),
        (RunStatus.SCRAPE_FAILED, NO_DATA_PREFIX, MAX_ATTEMPTS[FAILURE_UNAVAILABLE] - 1, False),
        (RunStatus.SCRAPE_FAILED, NO_DATA_PREFIX, MAX_ATTEMPTS[FAILURE_UNAVAILABLE], True),
        (RunStatus.SCRAPE_FAILED, "rank 3 is missing cells", MAX_ATTEMPTS[FAILURE_MALFORMED], True),
    ],
)
def test_attempts_run_out_by_class(status, error, attempts, stopped) -> None:  # noqa: ANN001
    failure_class = failure_class_of(_failed(status, error))
    retry = WeekRetry(attempts=attempts, next_retry_at=THEN)

    assert gave_up(retry, failure_class) is stopped
    assert retry_is_due(retry, failure_class, LATER) is not stopped


async def test_a_healthy_check_retries_a_failed_week_with_nobody_there(tmp_path: Path) -> None:
    """The finding. W31 failed in an outage, so it is no hole, and nothing went back to it."""
    reports = ReportsStore(tmp_path)
    reports.save(_failed(RunStatus.RADARR_FAILED, "Radarr refused"))
    backfill = _Backfill()
    scheduler = _scheduler(tmp_path, _Pipeline(_report(THEN)), backfill=backfill)

    await scheduler._run_scheduled()

    assert backfill.started == [[FAILED_WEEK]]


async def test_a_failed_check_retries_nothing_and_spends_nothing(tmp_path: Path) -> None:
    reports = ReportsStore(tmp_path)
    reports.save(_failed(RunStatus.RADARR_FAILED, "Radarr refused"))
    backfill = _Backfill()
    failing = _report(THEN, status=RunStatus.RADARR_FAILED)
    scheduler = _scheduler(tmp_path, _Pipeline(failing), backfill=backfill)

    await scheduler._run_scheduled()

    assert backfill.started == []
    assert IngestionStateStore(tmp_path).week_retries() == {}


async def test_one_pass_stays_inside_the_weekly_cap(tmp_path: Path) -> None:
    reports = ReportsStore(tmp_path)
    for week in range(10, 30):
        reports.save(Report(
            id=f"report-2026W{week}", run_at=THEN.isoformat(), trigger=RunTrigger.MANUAL,
            status=RunStatus.RADARR_FAILED, week=f"2026W{week}",
            totals=ReportTotals(movies=0, matched=0), error="down",
        ))
    backfill = _Backfill()
    scheduler = _scheduler(tmp_path, _Pipeline(_report(THEN)), backfill=backfill)

    await scheduler._run_scheduled()

    assert backfill.started[0] == [f"2026W{week}" for week in range(10, 22)]
