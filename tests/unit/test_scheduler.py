"""Step 15 test: run-now triggers manual, scheduled triggers scheduled, live reschedule."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.audit import AuditLog
from app.services.backup import BackupError
from app.services.filters import SCHEDULE_MODE_INTERVAL
from app.services.reports import Report, ReportTotals, RunStatus
from app.services.scheduler import (
    BACKUP_JOB_ID,
    MAX_JITTER_SECONDS,
    BoxMediaScheduler,
    _jitter_for,
)

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
