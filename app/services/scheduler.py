"""Weekly scheduler with live rescheduling (Step 15).

Wraps the pipeline in an APScheduler interval job so BoxMedia runs unattended —
the "zero user interaction" promise. Changing the interval in Settings reschedules
the running job in place (no container restart — the "hot-reload" behavior
`BoxMedia.md` describes). "Run now" is the manual trigger that explains the two
same-day reports in the weekly-view mockup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.audit import AuditAction, AuditLog
from app.services.backup import DEFAULT_KEEP, BackupError, BackupService
from app.services.filters import SCHEDULE_MODE_CADENCE
from app.services.pipeline import Pipeline
from app.services.reports import Report, ReportsStore, RunTrigger

JOB_ID = "weekly-box-office"
BACKUP_JOB_ID = "scheduled-backup"
SCHEDULED_BACKUP_REASON = "scheduled"
SECONDS_PER_HOUR = 3600
# Politeness / anti-fingerprinting for the Box Office Mojo scrape (improvement #3):
# spread each run by up to this many seconds, capped so short intervals stay sane.
MAX_JITTER_SECONDS = 3600
# How long after startup an overdue run may fire. Not immediately: a container that is
# restarting repeatedly would otherwise scrape Box Office Mojo on every boot, which is
# exactly the politeness the jitter above exists to protect.
STARTUP_GRACE_SECONDS = 120
# When the cadence fires. Box Office Mojo publishes a week in stages: early estimates on
# Sunday, actuals on Monday, revisions midweek, and the finished weekly chart by Friday —
# so four fetches catch a week's figures as they settle instead of freezing whichever
# stage a single fire happened to land on. Re-fetching an unchanged chart costs one page
# and writes nothing, which is what makes four affordable.
CADENCE_DAY_OF_WEEK = "sun,mon,wed,fri"
# Late enough in UTC to clear the US evening those Sunday estimates land in. Explicitly
# UTC rather than the scheduler's local default, so a container with TZ set cannot quietly
# fire at a different moment than the constant's name promises.
CADENCE_HOUR_UTC = 23
# A cadence catch-up fires once when the schedule has been silent this long — the one
# property `_first_run_at` gives interval mode that a cron trigger has no need of.
CADENCE_CATCHUP_AFTER_DAYS = 7
CATCHUP_JOB_ID = f"{JOB_ID}-catchup"


def _jitter_for(interval_hours: int) -> int:
    return min(MAX_JITTER_SECONDS, interval_hours * SECONDS_PER_HOUR // 4)


class BoxMediaScheduler:
    def __init__(
        self,
        pipeline: Pipeline,
        *,
        interval_hours: int,
        schedule_mode: str = SCHEDULE_MODE_CADENCE,
        backups: BackupService | None = None,
        backup_interval_days: int = 0,
        backup_keep: int = DEFAULT_KEEP,
        audit: AuditLog | None = None,
        reports: ReportsStore | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._interval_hours = interval_hours
        self._schedule_mode = schedule_mode
        # Optional: without a backup service (or with interval 0) the scheduler simply
        # runs the weekly chart job and nothing else.
        self._backups = backups
        self._backup_interval_days = backup_interval_days
        self._backup_keep = backup_keep
        # Optional so tests can build a scheduler without one; the app always passes it.
        self._audit = audit
        # Used only to answer "when did a scheduled run last happen" — see _first_run_at.
        self._reports = reports
        self._scheduler = AsyncIOScheduler()

    def last_run_at(self, reports: list[Report] | None = None) -> datetime | None:
        """When a scheduled run last happened, or None if one never has.

        Public because the reports page shows it: the schedule's own record of whether it
        has ever fired belongs beside the promise of when it fires next.

        `reports` is the list the caller already holds, following the same convention
        ReportsStore.histories and completed_weeks use — the page would otherwise read the
        history directory a second time to answer a question it just loaded the data for.
        """
        return self._last_scheduled_run(reports)

    def _last_scheduled_run(self, reports: list[Report] | None = None) -> datetime | None:
        """The stored answer, or None.

        Read from the reports rather than a separate state file: every run writes one,
        including a failed scrape, so this is already the record of "we went to Mojo".
        Counting a failure keeps a broken scraper from being retried on every restart.
        """
        if reports is None:
            if self._reports is None:
                return None
            reports = self._reports.list_reports()
        for report in reports:  # newest first
            if report.trigger != RunTrigger.SCHEDULED:
                continue
            try:
                return datetime.fromisoformat(report.run_at)
            except ValueError:
                continue  # a hand-edited timestamp is not a reason to never run again
        return None

    def _first_run_at(self) -> datetime:
        """When the weekly job should next fire, anchored on the last run.

        An interval trigger on its own puts the first run a WHOLE interval away and
        every restart resets that clock — so an install restarted more often than its
        interval (a NAS applying updates, an image pull, a crash loop) never ran at all,
        silently, while the page kept showing a confident "next check" date that moved
        further away each time. Anchoring on the last scheduled run makes a restart
        resume the schedule instead of postponing it.
        """
        soon = datetime.now(UTC) + timedelta(seconds=STARTUP_GRACE_SECONDS)
        last = self._last_scheduled_run()
        if last is None:
            return soon  # never run: a fresh install should not wait a full interval
        due = last + timedelta(hours=self._interval_hours)
        return max(due, soon)  # overdue runs shortly after startup, never instantly

    @property
    def interval_hours(self) -> int:
        return self._interval_hours

    @property
    def schedule_mode(self) -> str:
        return self._schedule_mode

    @property
    def backup_interval_days(self) -> int:
        return self._backup_interval_days

    def start(self) -> None:
        self._add_chart_job()
        self._apply_backup_job()
        self._scheduler.start()

    def _add_chart_job(self) -> None:
        """Register the weekly job for the current mode.

        Interval mode anchors the first run on the last one — `_first_run_at` exists
        because an interval trigger otherwise resets its clock on every restart. A cron
        trigger has no such clock: next Sunday is next Sunday however many times the
        container has restarted, so cadence mode sets no `next_run_time` and takes the
        catch-up below for the one property anchoring also provided.

        Jitter either way. It is the politeness that keeps every install from arriving at
        Box Office Mojo on the same second, and the cadence needs it MORE than an interval
        does, because every install on it shares the same four hours.
        """
        if self._schedule_mode == SCHEDULE_MODE_CADENCE:
            self._scheduler.add_job(
                self._run_scheduled,
                trigger=CronTrigger(
                    day_of_week=CADENCE_DAY_OF_WEEK,
                    hour=CADENCE_HOUR_UTC,
                    timezone=UTC,
                    jitter=MAX_JITTER_SECONDS,
                ),
                id=JOB_ID,
                replace_existing=True,
            )
            self._apply_catchup_job()
            return
        self._scheduler.add_job(
            self._run_scheduled,
            trigger="interval",
            hours=self._interval_hours,
            jitter=_jitter_for(self._interval_hours),
            id=JOB_ID,
            replace_existing=True,
            next_run_time=self._first_run_at(),
        )

    def _apply_catchup_job(self) -> None:
        """One extra run shortly after startup when the schedule has been silent.

        A fresh install would otherwise wait until the next Sunday before recording
        anything, and one that was switched off for a fortnight would come back with a
        hole it never tries to fill. Not immediate — the same startup grace interval mode
        uses, so a crash-looping container does not scrape on every boot.
        """
        last = self._last_scheduled_run()
        overdue = last is None or datetime.now(UTC) - last > timedelta(
            days=CADENCE_CATCHUP_AFTER_DAYS
        )
        if not overdue:
            return
        self._scheduler.add_job(
            self._run_scheduled,
            trigger=DateTrigger(
                run_date=datetime.now(UTC) + timedelta(seconds=STARTUP_GRACE_SECONDS)
            ),
            id=CATCHUP_JOB_ID,
            replace_existing=True,
        )

    def _apply_backup_job(self) -> None:
        """Add, update, or remove the backup job to match the configured interval."""
        wanted = self._backups is not None and self._backup_interval_days > 0
        existing = self._scheduler.get_job(BACKUP_JOB_ID)
        if not wanted:
            if existing is not None:
                self._scheduler.remove_job(BACKUP_JOB_ID)
            return
        self._scheduler.add_job(
            self._run_backup,
            trigger="interval",
            days=self._backup_interval_days,
            id=BACKUP_JOB_ID,
            replace_existing=True,
        )

    async def _run_scheduled(self) -> None:
        await self._pipeline.run(trigger=RunTrigger.SCHEDULED)

    async def _run_backup(self) -> None:
        """Take an unattended encrypted snapshot, pruned to the configured retention.

        `create` is synchronous and CPU/IO bound (tar + gzip + AES over the whole data
        tree) and holds the global write lock, so it runs in a worker thread rather than
        stalling the event loop. A failure is recorded and swallowed — a missed backup
        must not kill the scheduler.

        OSError is caught alongside BackupError because that is what the create path
        actually raises: every BackupError in the backup service comes from the restore
        helpers, while a full disk surfaces as OSError out of `atomic_write_bytes`. A
        programming error is deliberately NOT caught — that should still surface loudly.
        """
        if self._backups is None:
            return
        try:
            await asyncio.to_thread(
                self._backups.create,
                keep=self._backup_keep,
                reason=SCHEDULED_BACKUP_REASON,
            )
        except (BackupError, OSError) as exc:
            # The audit log is where an admin can see this; the print stays so it is also
            # in `docker logs` for anyone already tailing them.
            if self._audit is not None:
                self._audit.record(
                    AuditAction.BACKUP_FAILED,
                    reason=SCHEDULED_BACKUP_REASON,
                    error=str(exc),
                )
            print(f"scheduled backup failed: {exc}", flush=True)

    async def run_now(self) -> Report:
        return await self._pipeline.run(trigger=RunTrigger.MANUAL)

    def reschedule(
        self,
        interval_hours: int,
        *,
        schedule_mode: str | None = None,
        backup_interval_days: int | None = None,
        backup_keep: int | None = None,
    ) -> None:
        """Apply new settings to the running jobs. Arguments left as None keep what the
        scheduler already has, so saving one Settings form cannot disturb another's."""
        self._interval_hours = interval_hours
        if schedule_mode is not None:
            self._schedule_mode = schedule_mode
        if backup_interval_days is not None:
            self._backup_interval_days = backup_interval_days
        if backup_keep is not None:
            self._backup_keep = backup_keep
        if self._scheduler.running:
            # Re-added rather than rescheduled: the two modes need different trigger
            # types, and `reschedule_job` only swaps the trigger of the job already there.
            # `replace_existing` makes this one operation for both, and `_add_chart_job`
            # keeps the anchoring interval mode needs — reschedule_job recomputes from
            # NOW, so without it every save pushed the scrape a full interval away.
            self._add_chart_job()
            self._apply_backup_job()

    def next_run_at(self) -> datetime | None:
        """When the weekly job fires next, or None before `start()` has scheduled it.

        Read live from APScheduler rather than computed from the interval, so the answer
        already includes the per-run jitter.
        """
        job = self._scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

    def job_interval_hours(self) -> float | None:
        """The live interval of the scheduled job, or None when it has none.

        A cadence job is a cron trigger and has no interval to report — a fact about the
        schedule rather than a missing job, so the pages that describe it ask
        `schedule_mode` instead of inferring one from this.
        """
        job = self._scheduler.get_job(JOB_ID)
        interval = getattr(job.trigger, "interval", None) if job else None
        return interval.total_seconds() / 3600 if interval else None

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
