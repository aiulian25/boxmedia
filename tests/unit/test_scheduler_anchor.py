"""The weekly job must survive a restart instead of postponing itself forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.reports import (
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.scheduler import STARTUP_GRACE_SECONDS, BoxMediaScheduler

INTERVAL_HOURS = 168


def _store(tmp_path: Path, *runs: tuple[str, datetime]) -> ReportsStore:
    store = ReportsStore(tmp_path)
    for index, (trigger, moment) in enumerate(runs):
        store.save(Report(
            id=f"report-2026W{index + 1:02d}-{index:03d}", run_at=moment.isoformat(),
            trigger=trigger, status=RunStatus.OK, week=f"2026W{index + 1:02d}",
            totals=ReportTotals(movies=0, matched=0),
        ))
    return store


def _scheduler(store: ReportsStore | None) -> BoxMediaScheduler:
    return BoxMediaScheduler(None, interval_hours=INTERVAL_HOURS, reports=store)


def test_a_fresh_install_does_not_wait_a_whole_interval(tmp_path: Path) -> None:
    """An interval trigger alone puts the first run a week out, so a new install sat
    idle for a week before its first automatic check."""
    first = _scheduler(_store(tmp_path))._first_run_at()

    assert first - datetime.now(UTC) < timedelta(seconds=STARTUP_GRACE_SECONDS + 30)


def test_a_restart_resumes_the_schedule_rather_than_postponing_it(tmp_path: Path) -> None:
    """The reported bug: every restart reset the clock, so an install restarted more
    often than weekly never ran at all."""
    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    first = _scheduler(_store(tmp_path, (RunTrigger.SCHEDULED, two_days_ago)))._first_run_at()

    # Five days left of the seven, not seven again.
    assert timedelta(days=4, hours=20) < first - datetime.now(UTC) < timedelta(days=5, hours=4)


def test_an_overdue_run_fires_shortly_after_startup(tmp_path: Path) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    first = _scheduler(_store(tmp_path, (RunTrigger.SCHEDULED, long_ago)))._first_run_at()

    assert first - datetime.now(UTC) < timedelta(seconds=STARTUP_GRACE_SECONDS + 30)


def test_an_overdue_run_still_waits_out_the_startup_grace(tmp_path: Path) -> None:
    """Never instantly: a crash-looping container would otherwise scrape Mojo on every
    boot, which is what the jitter exists to avoid."""
    long_ago = datetime.now(UTC) - timedelta(days=30)
    first = _scheduler(_store(tmp_path, (RunTrigger.SCHEDULED, long_ago)))._first_run_at()

    assert first > datetime.now(UTC) + timedelta(seconds=STARTUP_GRACE_SECONDS - 30)


def test_manual_runs_do_not_count_as_a_scheduled_check(tmp_path: Path) -> None:
    """Pressing Run Now must not make the app think its unattended check happened —
    the install this was found on had 13 manual runs and zero scheduled ones."""
    store = _store(tmp_path, (RunTrigger.MANUAL, datetime.now(UTC)))

    first = _scheduler(store)._first_run_at()

    assert first - datetime.now(UTC) < timedelta(seconds=STARTUP_GRACE_SECONDS + 30)


def test_a_failed_scheduled_run_still_counts_as_having_run(tmp_path: Path) -> None:
    """Otherwise a broken scraper is retried on every restart."""
    store = ReportsStore(tmp_path)
    store.save(Report(
        id="report-2026W30-failed", run_at=datetime.now(UTC).isoformat(),
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED, week="2026W30",
        totals=ReportTotals(movies=0, matched=0), error="layout changed",
    ))

    first = _scheduler(store)._first_run_at()

    assert first - datetime.now(UTC) > timedelta(days=6)


def test_without_a_reports_store_it_still_schedules(tmp_path: Path) -> None:
    # Tests build schedulers without one; that must not crash or block startup.
    assert _scheduler(None)._first_run_at() > datetime.now(UTC)
