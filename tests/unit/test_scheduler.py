"""Step 15 test: run-now triggers manual, scheduled triggers scheduled, live reschedule."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.audit import AuditLog
from app.services.backup import BackupError
from app.services.filters import SCHEDULE_MODE_INTERVAL
from app.services.reports import (
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.scheduler import (
    BACKUP_JOB_ID,
    CALENDAR_JOB_ID,
    DAILY_REFRESH_HOUR_UTC,
    DAILY_REFRESH_JITTER_SECONDS,
    MAX_JITTER_SECONDS,
    SERIES_JOB_ID,
    BoxMediaScheduler,
    _jitter_for,
)
from app.services.sonarr import SonarrError

WEEKLY_HOURS = 168


class StubPipeline:
    def __init__(self) -> None:
        self.triggers: list[str] = []

    async def run(self, *, trigger: str) -> Report:
        self.triggers.append(trigger)
        return Report(
            id="report-stub", run_at="2026-08-12T00:00:00+00:00", trigger=trigger,
            status=RunStatus.OK, totals=ReportTotals(movies=0, matched=0),
        )


async def test_run_now_triggers_manual() -> None:
    pipeline = StubPipeline()
    scheduler = BoxMediaScheduler(pipeline, interval_hours=WEEKLY_HOURS)
    report = await scheduler.run_now()
    assert pipeline.triggers == ["manual"]
    assert report.trigger == "manual"


async def test_scheduled_run_triggers_scheduled() -> None:
    pipeline = StubPipeline()
    scheduler = BoxMediaScheduler(pipeline, interval_hours=WEEKLY_HOURS)
    await scheduler._run_scheduled()
    assert pipeline.triggers == ["scheduled"]


async def test_reschedule_changes_interval_without_restart() -> None:
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler.start()
    try:
        assert scheduler.job_interval_hours() == WEEKLY_HOURS
        scheduler.reschedule(24)
        assert scheduler.interval_hours == 24
        assert scheduler.job_interval_hours() == 24  # live job updated, no restart
    finally:
        scheduler.shutdown()


def test_jitter_capped_and_scaled() -> None:
    # Weekly interval is capped at the max; a short interval scales down.
    assert _jitter_for(WEEKLY_HOURS) == MAX_JITTER_SECONDS
    assert _jitter_for(1) == 900  # 3600 // 4


async def test_scheduled_job_has_jitter_applied() -> None:
    scheduler = BoxMediaScheduler(StubPipeline(), interval_hours=WEEKLY_HOURS)
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job("weekly-box-office")
        assert job.trigger.jitter == MAX_JITTER_SECONDS
    finally:
        scheduler.shutdown()


class StubBackups:
    """Stands in for BackupService: records how it was asked to snapshot."""

    def __init__(self, fails: bool = False) -> None:
        self.calls: list[dict] = []
        self.fails = fails

    def create(self, *, keep: int, reason: str) -> str:
        self.calls.append({"keep": keep, "reason": reason})
        if self.fails:
            raise BackupError("disk full")
        return "boxmedia-stub.backup"


async def test_no_backup_job_when_interval_is_zero() -> None:
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, backups=StubBackups()
    )
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job(BACKUP_JOB_ID) is None
    finally:
        scheduler.shutdown()


async def test_backup_job_scheduled_when_interval_set() -> None:
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS,
        backups=StubBackups(), backup_interval_days=1,
    )
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job(BACKUP_JOB_ID)
        assert job is not None
        assert job.trigger.interval.days == 1
    finally:
        scheduler.shutdown()


async def test_scheduled_backup_uses_the_configured_retention() -> None:
    backups = StubBackups()
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS,
        backups=backups, backup_interval_days=1, backup_keep=3,
    )
    await scheduler._run_backup()
    assert backups.calls == [{"keep": 3, "reason": "scheduled"}]


async def test_a_failed_backup_does_not_kill_the_job() -> None:
    backups = StubBackups(fails=True)
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, backups=backups, backup_interval_days=1
    )
    await scheduler._run_backup()  # must not raise
    assert backups.calls  # it did try


async def test_reschedule_turns_backups_on_and_off_live() -> None:
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, backups=StubBackups()
    )
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job(BACKUP_JOB_ID) is None
        scheduler.reschedule(WEEKLY_HOURS, backup_interval_days=7, backup_keep=5)
        assert scheduler._scheduler.get_job(BACKUP_JOB_ID) is not None
        scheduler.reschedule(WEEKLY_HOURS, backup_interval_days=0)
        assert scheduler._scheduler.get_job(BACKUP_JOB_ID) is None  # turned back off
    finally:
        scheduler.shutdown()


async def test_next_run_at_is_none_before_start() -> None:
    scheduler = BoxMediaScheduler(StubPipeline(), interval_hours=WEEKLY_HOURS)
    assert scheduler.next_run_at() is None


async def test_next_run_at_reports_the_scheduled_job() -> None:
    scheduler = BoxMediaScheduler(StubPipeline(), interval_hours=WEEKLY_HOURS)
    scheduler.start()
    try:
        next_run = scheduler.next_run_at()
        assert next_run is not None
        # Within the interval plus its jitter — proves it's the live job, not a constant.
        ahead = next_run - datetime.now(next_run.tzinfo)
        assert 0 < ahead.total_seconds() <= WEEKLY_HOURS * 3600 + MAX_JITTER_SECONDS
    finally:
        scheduler.shutdown()


class FailingBackups:
    """A backup service whose create() raises whatever the test hands it."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def create(self, **kwargs: object) -> str:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        BackupError("archive could not be written"),
        # The realistic one: a full disk surfaces as OSError out of atomic_write_bytes,
        # and every BackupError in the backup service comes from the RESTORE helpers.
        OSError(28, "No space left on device"),
    ],
)
async def test_a_failed_scheduled_backup_is_audited(
    tmp_path: Path, error: Exception
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    scheduler = BoxMediaScheduler(
        StubPipeline(),
        interval_hours=WEEKLY_HOURS,
        backups=FailingBackups(error),
        backup_interval_days=1,
        audit=audit,
    )

    await scheduler._run_backup()  # must not raise — a missed backup cannot kill the job

    entries = [entry for entry in audit.tail(10) if entry["action"] == "backup_failed"]
    assert len(entries) == 1
    assert entries[0]["reason"] == "scheduled"
    assert str(error) in entries[0]["error"]


async def test_a_successful_scheduled_backup_records_no_failure(tmp_path: Path) -> None:
    class Working:
        def create(self, **kwargs: object) -> str:
            return "boxmedia-20260814-000000-aaaa.backup"

    audit = AuditLog(tmp_path / "audit.jsonl")
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, backups=Working(),
        backup_interval_days=1, audit=audit,
    )
    await scheduler._run_backup()
    assert [entry for entry in audit.tail(10) if entry["action"] == "backup_failed"] == []


async def test_a_programming_error_is_not_swallowed(tmp_path: Path) -> None:
    # Environment failures are recorded and survived; a bug should still surface loudly
    # rather than being logged as "backup failed" forever.
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS,
        backups=FailingBackups(TypeError("bad call")), backup_interval_days=1,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    with pytest.raises(TypeError):
        await scheduler._run_backup()


async def test_the_scheduler_still_works_without_an_audit_handle(tmp_path: Path) -> None:
    # The parameter is optional; a scheduler built without one must not crash on failure.
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS,
        backups=FailingBackups(BackupError("nope")), backup_interval_days=1,
    )
    await scheduler._run_backup()


class StubRefresher:
    """Stands in for ServerRefresher: records what the morning jobs asked it to do."""

    def __init__(self, complete: bool = True, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.complete = complete
        self.error = error

    async def calendar(self) -> bool:
        self.calls.append("calendar")
        if self.error is not None:
            raise self.error
        return self.complete

    async def series(self) -> bool:
        self.calls.append("series")
        if self.error is not None:
            raise self.error
        return self.complete


def _with_refresher(refresher: object, **overrides: object) -> BoxMediaScheduler:
    return BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS, refresher=refresher, **overrides
    )


# --- TV step 15: the morning jobs ---


async def test_both_morning_jobs_fire_daily_at_six_utc() -> None:
    scheduler = _with_refresher(StubRefresher())
    scheduler.start()
    try:
        for job_id in (CALENDAR_JOB_ID, SERIES_JOB_ID):
            job = scheduler._scheduler.get_job(job_id)
            assert job is not None, job_id
            fields = {field.name: str(field) for field in job.trigger.fields}
            assert fields["hour"] == str(DAILY_REFRESH_HOUR_UTC)
            # Every day: a calendar that is a day stale is a calendar nobody trusts.
            assert fields["day_of_week"] == "*"
            assert str(job.trigger.timezone) == "UTC"
    finally:
        scheduler.shutdown()


async def test_the_morning_jobs_are_jittered_like_everything_else() -> None:
    scheduler = _with_refresher(StubRefresher())
    scheduler.start()
    try:
        for job_id in (CALENDAR_JOB_ID, SERIES_JOB_ID):
            job = scheduler._scheduler.get_job(job_id)
            assert job.trigger.jitter == DAILY_REFRESH_JITTER_SECONDS
    finally:
        scheduler.shutdown()


async def test_no_morning_jobs_without_a_refresher() -> None:
    """A bare scheduler runs the weekly chart job and nothing else — the same shape the
    backup service already has."""
    scheduler = BoxMediaScheduler(StubPipeline(), interval_hours=WEEKLY_HOURS)
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job(CALENDAR_JOB_ID) is None
        assert scheduler._scheduler.get_job(SERIES_JOB_ID) is None
    finally:
        scheduler.shutdown()


async def test_the_morning_jobs_get_no_catchup() -> None:
    """Unlike a missed week, a missed refresh is not a hole: the page re-reads on open
    when its cache is stale. Catching up would spend a burst of requests at boot to buy
    back what the next page view buys for free."""
    scheduler = _with_refresher(StubRefresher())
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job(f"{CALENDAR_JOB_ID}-catchup") is None
        assert scheduler._scheduler.get_job(f"{SERIES_JOB_ID}-catchup") is None
    finally:
        scheduler.shutdown()


async def test_saving_settings_leaves_the_morning_jobs_alone() -> None:
    """Reschedule-on-save touches the chart and backup jobs, which is what the form
    changes. The morning pair has no setting behind it and must simply keep firing."""
    scheduler = _with_refresher(StubRefresher(), backups=StubBackups())
    scheduler.start()
    try:
        before = scheduler.next_calendar_run_at()
        scheduler.reschedule(24, backup_interval_days=2)
        job = scheduler._scheduler.get_job(CALENDAR_JOB_ID)
        assert job is not None
        assert scheduler.next_calendar_run_at() == before
        assert scheduler._scheduler.get_job(SERIES_JOB_ID) is not None
    finally:
        scheduler.shutdown()


async def test_the_calendar_job_refreshes_the_calendar() -> None:
    refresher = StubRefresher()
    await _with_refresher(refresher)._run_calendar_refresh()
    assert refresher.calls == ["calendar"]


async def test_the_series_job_refreshes_the_series_snapshot() -> None:
    refresher = StubRefresher()
    await _with_refresher(refresher)._run_series_refresh()
    assert refresher.calls == ["series"]


async def test_a_scheduled_calendar_refresh_is_audited(tmp_path: Path) -> None:
    """A page states a claim from this — "Last fetch: …" — and the audit log is where an
    admin checks that claim against what actually ran."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    scheduler = _with_refresher(StubRefresher(complete=False), audit=audit)

    await scheduler._run_calendar_refresh()

    rows = [row for row in audit.tail(10) if row["action"] == "calendar_refreshed"]
    assert len(rows) == 1
    assert rows[0]["complete"] is False


async def test_the_series_job_writes_no_audit_row(tmp_path: Path) -> None:
    """It backs no visible claim about freshness, and a second daily row would be noise in
    a log that is read for sign-ins and key changes."""
    audit = AuditLog(tmp_path / "audit.jsonl")

    await _with_refresher(StubRefresher(), audit=audit)._run_series_refresh()

    assert audit.tail(10) == []


@pytest.mark.parametrize(
    "error",
    [
        OSError(28, "No space left on device"),  # the realistic one: the cache write
        SonarrError("connection reset"),
    ],
)
async def test_a_failed_morning_job_is_recorded_and_survived(
    tmp_path: Path, error: Exception
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    scheduler = _with_refresher(StubRefresher(error=error), audit=audit)

    await scheduler._run_calendar_refresh()  # must not raise — one bad morning is not fatal

    rows = [row for row in audit.tail(10) if row["action"] == "refresh_failed"]
    assert len(rows) == 1
    assert rows[0]["job"] == CALENDAR_JOB_ID
    assert str(error) in rows[0]["error"]
    # And the claim it would otherwise have made is NOT written down.
    assert [row for row in audit.tail(10) if row["action"] == "calendar_refreshed"] == []


async def test_a_programming_error_in_a_morning_job_is_not_swallowed(
    tmp_path: Path,
) -> None:
    """Environment failures are survived; a bug should still surface loudly rather than
    being logged as "refresh failed" every morning — the backup job's own rule."""
    scheduler = _with_refresher(
        StubRefresher(error=TypeError("bad call")), audit=AuditLog(tmp_path / "audit.jsonl")
    )
    with pytest.raises(TypeError):
        await scheduler._run_calendar_refresh()


async def test_a_failing_series_job_is_recorded_under_its_own_name(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")

    await _with_refresher(
        StubRefresher(error=OSError("disk")), audit=audit
    )._run_series_refresh()

    rows = [row for row in audit.tail(10) if row["action"] == "refresh_failed"]
    assert rows[0]["job"] == SERIES_JOB_ID


async def test_next_calendar_run_at_is_none_before_start() -> None:
    assert _with_refresher(StubRefresher()).next_calendar_run_at() is None


async def test_next_calendar_run_at_reports_the_morning_job() -> None:
    """What the calendar page prints beside the last fetch."""
    scheduler = _with_refresher(StubRefresher())
    scheduler.start()
    try:
        next_run = scheduler.next_calendar_run_at()
        assert next_run is not None
        assert next_run > datetime.now(UTC)
    finally:
        scheduler.shutdown()


async def test_a_morning_job_without_a_refresher_does_nothing_rather_than_crashing() -> None:
    """The bodies are reachable through APScheduler's own machinery; neither may assume
    the optional collaborator is there."""
    scheduler = BoxMediaScheduler(StubPipeline(), interval_hours=WEEKLY_HOURS)
    await scheduler._run_calendar_refresh()
    await scheduler._run_series_refresh()


async def test_nothing_reaches_a_third_party_unattended() -> None:
    """The whole registered set, pinned. Discover deliberately has no job — its TTL and
    its Refresh button are enough, and unattended traffic to Trakt and TMDB should stay at
    zero. This fails the moment a job is added that would change that, which is the point:
    the decision is easy to make again by accident.
    """
    scheduler = _with_refresher(StubRefresher(), backups=StubBackups(), backup_interval_days=1)
    scheduler.start()
    try:
        registered = {job.id for job in scheduler._scheduler.get_jobs()}
        assert registered <= {
            "weekly-box-office", "weekly-box-office-catchup",
            BACKUP_JOB_ID, CALENDAR_JOB_ID, SERIES_JOB_ID,
        }
        assert CALENDAR_JOB_ID in registered and SERIES_JOB_ID in registered
    finally:
        scheduler.shutdown()


# --- headless: a scheduled run fills the weeks the history skipped ---


class _RecordingBackfill:
    def __init__(self) -> None:
        self.started: list[list[str]] = []

    def start(self, weeks: list[str]) -> bool:
        self.started.append(list(weeks))
        return True


class _FailingPipeline:
    async def run(self, *, trigger: str) -> Report:
        return Report(
            id="report-stub-failed", run_at="2026-10-02T00:00:00+00:00", trigger=trigger,
            status=RunStatus.RADARR_FAILED, totals=ReportTotals(movies=0, matched=0),
        )


def _history_with_a_hole(tmp_path: Path) -> ReportsStore:
    """Weeks 33 and 36 stored; 34 and 35 came and went while the container was down."""
    store = ReportsStore(tmp_path / "history")
    for week in ("2026W33", "2026W36"):
        store.save(Report(
            id=f"report-{week}", run_at="2026-09-01T00:00:00+00:00",
            trigger=RunTrigger.SCHEDULED, status=RunStatus.OK, week=week,
            totals=ReportTotals(movies=1, matched=1),
        ))
    return store


async def test_a_healthy_scheduled_run_fetches_the_weeks_the_history_skipped(
    tmp_path: Path,
) -> None:
    """The app is a headless container: nobody is there to press "Fetch missing weeks",
    so after an outage the schedule has to close the hole on its own."""
    backfill = _RecordingBackfill()
    scheduler = BoxMediaScheduler(
        StubPipeline(), interval_hours=WEEKLY_HOURS,
        reports=_history_with_a_hole(tmp_path), backfill=backfill,
    )

    await scheduler._run_scheduled()

    assert backfill.started == [["2026W34", "2026W35"]]


async def test_a_failed_scheduled_run_fetches_nothing_else(tmp_path: Path) -> None:
    """A failed run usually means Radarr or Mojo is unreachable. Filling then would record
    every missing week as failed, and a failed week is never offered again — the outage
    would cost every week it overlapped, permanently."""
    backfill = _RecordingBackfill()
    scheduler = BoxMediaScheduler(
        _FailingPipeline(), interval_hours=WEEKLY_HOURS,
        reports=_history_with_a_hole(tmp_path), backfill=backfill,
    )

    await scheduler._run_scheduled()

    assert backfill.started == []
