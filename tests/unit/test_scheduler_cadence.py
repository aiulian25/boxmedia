"""M4: the unattended check follows Mojo's rhythm rather than a stopwatch.

Mojo does not publish a week and stop. It posts an estimate, then settles it over the days
that follow, so a single weekly poll records whichever version of the week it happened to
land on. Four checks across the settling window record the last one instead — and because
the pipeline already refuses to write a chart it has seen before, three of the four cost
nothing but a page fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.filters import (
    SCHEDULE_MODE_CADENCE,
    SCHEDULE_MODE_INTERVAL,
    FiltersConfig,
)
from app.services.reports import (
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.scheduler import (
    CADENCE_CATCHUP_AFTER_DAYS,
    CADENCE_DAY_OF_WEEK,
    CADENCE_HOUR_UTC,
    CATCHUP_JOB_ID,
    JOB_ID,
    MAX_JITTER_SECONDS,
    STARTUP_GRACE_SECONDS,
    BoxMediaScheduler,
)

INTERVAL_HOURS = 168
# The four days named literally. Every other test derives them from the constant, which is
# self-consistent at any value — this is the one that would fail if the cadence quietly
# became "sun" alone or "mon,tue,wed,thu,fri,sat,sun".
EXPECTED_DAYS = ["sun", "mon", "wed", "fri"]


def _store(tmp_path: Path, *runs: tuple[str, datetime]) -> ReportsStore:
    store = ReportsStore(tmp_path)
    for index, (trigger, moment) in enumerate(runs):
        store.save(Report(
            id=f"report-2026W{index + 1:02d}-{index:03d}", run_at=moment.isoformat(),
            trigger=trigger, status=RunStatus.OK, week=f"2026W{index + 1:02d}",
            totals=ReportTotals(movies=0, matched=0),
        ))
    return store


def _cadence(store: ReportsStore | None = None) -> BoxMediaScheduler:
    scheduler = BoxMediaScheduler(
        None, interval_hours=INTERVAL_HOURS,
        schedule_mode=SCHEDULE_MODE_CADENCE, reports=store,
    )
    scheduler._add_chart_job()
    return scheduler


def test_the_cadence_is_four_named_days_a_week() -> None:
    trigger = _cadence()._scheduler.get_job(JOB_ID).trigger

    days = str(next(f for f in trigger.fields if f.name == "day_of_week"))
    assert sorted(days.split(",")) == sorted(EXPECTED_DAYS)
    assert len(EXPECTED_DAYS) == 4, "four checks a week is the whole point of the mode"
    assert CADENCE_DAY_OF_WEEK.split(",") == EXPECTED_DAYS


def test_the_cadence_fires_late_enough_to_clear_the_us_evening() -> None:
    """Sunday's estimate lands on US evening time. A check at, say, 06:00 UTC would read
    Sunday's page before Sunday has finished happening."""
    trigger = _cadence()._scheduler.get_job(JOB_ID).trigger

    assert str(next(f for f in trigger.fields if f.name == "hour")) == str(CADENCE_HOUR_UTC)
    assert CADENCE_HOUR_UTC == 23
    # Explicit, not the container's local zone: a TZ env var must not move the run.
    assert str(trigger.timezone) == "UTC"


def test_the_cadence_still_jitters() -> None:
    """It needs this MORE than an interval does — every install on the cadence shares the
    same four hours, so without jitter they would all arrive together."""
    assert _cadence()._scheduler.get_job(JOB_ID).trigger.jitter == MAX_JITTER_SECONDS


def test_a_cadence_job_reports_no_interval() -> None:
    """There is no interval to report, which is a fact about the schedule rather than a
    missing job — the pages that describe it ask `schedule_mode` instead."""
    scheduler = _cadence()

    assert scheduler.job_interval_hours() is None
    assert scheduler._scheduler.get_job(JOB_ID) is not None


def test_interval_mode_is_untouched() -> None:
    scheduler = BoxMediaScheduler(
        None, interval_hours=48, schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler._add_chart_job()

    assert scheduler.job_interval_hours() == 48
    # No catch-up: interval mode anchors its first run instead, which is the same
    # property by another means.
    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is None


def test_a_fresh_install_does_not_wait_for_sunday(tmp_path: Path) -> None:
    """A cron trigger alone would leave a new install idle until the next Sunday."""
    scheduler = _cadence(_store(tmp_path))

    catchup = scheduler._scheduler.get_job(CATCHUP_JOB_ID)
    assert catchup is not None
    assert catchup.trigger.run_date - datetime.now(UTC) <= timedelta(
        seconds=STARTUP_GRACE_SECONDS + 5
    )


def test_the_catch_up_still_waits_out_the_startup_grace(tmp_path: Path) -> None:
    """A crash-looping container must not scrape on every boot."""
    scheduler = _cadence(_store(tmp_path))

    catchup = scheduler._scheduler.get_job(CATCHUP_JOB_ID)
    assert catchup.trigger.run_date - datetime.now(UTC) > timedelta(seconds=60)


def test_a_long_silence_is_caught_up(tmp_path: Path) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=CADENCE_CATCHUP_AFTER_DAYS + 1)
    scheduler = _cadence(_store(tmp_path, (RunTrigger.SCHEDULED, long_ago)))

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is not None


def test_a_recent_run_means_no_catch_up(tmp_path: Path) -> None:
    """Restarting a healthy install must not cost Box Office Mojo an extra fetch."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    scheduler = _cadence(_store(tmp_path, (RunTrigger.SCHEDULED, yesterday)))

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is None


def test_the_catch_up_threshold_is_a_week(tmp_path: Path) -> None:
    """Long enough that an ordinary restart is silent, short enough that a schedule which
    genuinely missed its window is noticed."""
    assert CADENCE_CATCHUP_AFTER_DAYS == 7

    just_inside = datetime.now(UTC) - timedelta(days=CADENCE_CATCHUP_AFTER_DAYS, hours=-1)
    scheduler = _cadence(_store(tmp_path, (RunTrigger.SCHEDULED, just_inside)))
    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is None


def test_a_manual_run_does_not_count_as_the_schedule_having_run(tmp_path: Path) -> None:
    """Someone pressing Run Now is not the unattended check working."""
    recent = datetime.now(UTC) - timedelta(hours=2)
    scheduler = _cadence(_store(tmp_path, (RunTrigger.MANUAL, recent)))

    assert scheduler._scheduler.get_job(CATCHUP_JOB_ID) is not None


async def test_switching_mode_live_replaces_the_trigger() -> None:
    """`reschedule_job` only swaps the trigger of the job already there, and the two modes
    need different trigger types."""
    scheduler = BoxMediaScheduler(
        None, interval_hours=INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler.start()
    try:
        assert scheduler.job_interval_hours() == INTERVAL_HOURS

        scheduler.reschedule(INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_CADENCE)
        assert scheduler.schedule_mode == SCHEDULE_MODE_CADENCE
        assert scheduler.job_interval_hours() is None
        assert scheduler.next_run_at() is not None

        scheduler.reschedule(24, schedule_mode=SCHEDULE_MODE_INTERVAL)
        assert scheduler.job_interval_hours() == 24
    finally:
        scheduler.shutdown()


async def test_rescheduling_without_a_mode_keeps_the_one_in_force() -> None:
    """Saving the backup cadence lands in the same call. It must not knock the chart
    schedule back to a default nobody chose."""
    scheduler = BoxMediaScheduler(
        None, interval_hours=INTERVAL_HOURS, schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler.start()
    try:
        scheduler.reschedule(INTERVAL_HOURS, backup_interval_days=3, backup_keep=5)

        assert scheduler.schedule_mode == SCHEDULE_MODE_INTERVAL
        assert scheduler.job_interval_hours() == INTERVAL_HOURS
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("stored", [None, "", "weekly", "mojo cadence", 7, True])
def test_a_stored_mode_this_build_does_not_ship_reads_as_the_default(stored: object) -> None:
    """Read-tolerant on the way in, the way a stored theme is: a config written by a newer
    build, or edited by hand, must not stop the container from starting."""
    assert FiltersConfig(schedule_mode=stored).schedule_mode == SCHEDULE_MODE_CADENCE


def test_the_default_is_the_cadence() -> None:
    """The mode most installs want without choosing: it is the one that actually matches
    how the source publishes."""
    assert FiltersConfig().schedule_mode == SCHEDULE_MODE_CADENCE
    assert BoxMediaScheduler(None, interval_hours=1).schedule_mode == SCHEDULE_MODE_CADENCE
