"""Filling the holes in stored history, one polite request at a time.

A gap is easy to make — the container was down, the schedule was off, the install is
younger than the weeks it wants — and until now the only way to close one was to pick a
date, POST, wait, and repeat per week. This runs that same loop unattended.

Deliberately NOT a scheduler job: it is a one-shot the admin asks for, it must be
single-flight, and it must die with the process. What it is not allowed to be is fast —
every week is another page fetched from Box Office Mojo, so the pause between them is the
point, and it is the same courtesy the weekly job's jitter exists for.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

from app.services.boxoffice import WeekNotPublishedError
from app.services.ingestion_state import IngestionStateStore
from app.services.maintenance import MaintenanceGate
from app.services.pipeline import Pipeline
from app.services.reports import RunStatus, RunTrigger

# The pause between weeks, plus up to JITTER seconds. Twelve weeks therefore takes three
# minutes at least — which is the intended shape: a backfill is something left to get on
# with, not something waited on.
BACKFILL_DELAY_SECONDS = 15.0
BACKFILL_JITTER_SECONDS = 5.0


class BackfillRunner:
    """One backfill at a time, with a status the reports page can render.

    Single-flight because two of these would double the request rate at Box Office Mojo
    and race to write the same weeks. `start` refuses rather than queues: the second click
    is almost always the same click, and a queue would let an impatient admin stack up
    exactly the traffic the delay exists to avoid.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        maintenance: MaintenanceGate | None = None,
        ingestion: IngestionStateStore | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._maintenance = maintenance or MaintenanceGate()
        # Where a failed week's retry count lives, so backoff survives a restart. Optional
        # so tests can build a runner without one; the app always passes it.
        self._ingestion = ingestion
        self._task: asyncio.Task | None = None
        self._weeks: list[str] = []
        self._done = 0
        self._failed = 0
        self._deferred = 0
        self._current: str | None = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, object]:
        """What the page shows. Safe to call at any time, including before a first run."""
        return {
            "running": self.running(),
            "done": self._done,
            "failed": self._failed,
            "deferred": self._deferred,
            "total": len(self._weeks),
            "current_week": self._current,
        }

    def start(self, weeks: list[str]) -> bool:
        """Begin a backfill. False when one is running, there is nothing to do, or a
        maintenance has the door closed.

        The caller passes the weeks; nothing here reads them from a request. They are
        derived server-side from stored history, so no submitted value can steer what gets
        fetched — the same rule the add route follows for connection ids.
        """
        if self.running() or not weeks or not self._maintenance.try_enter():
            return False
        self._weeks = list(weeks)
        self._done = 0
        self._failed = 0
        self._deferred = 0
        self._current = None
        self._task = asyncio.create_task(self._run())
        # A callback rather than a `finally`: a task cancelled before its first step never
        # enters `_run`, and the gate would wait for it for ever.
        self._task.add_done_callback(lambda _: self._maintenance.leave())
        return True

    async def _run(self) -> None:
        try:
            for index, week in enumerate(self._weeks):
                self._current = week
                await self._fetch(week)
                if index < len(self._weeks) - 1:
                    await asyncio.sleep(
                        BACKFILL_DELAY_SECONDS + random.uniform(0, BACKFILL_JITTER_SECONDS)  # noqa: S311
                    )
        finally:
            # Whatever happened — finished, cancelled at shutdown, or a bug — the page must
            # not go on claiming a week is in flight.
            self._current = None

    async def _fetch(self, week: str) -> None:
        """One week through the ordinary pipeline, so everything it does still applies.

        Dedupe, report-id reuse, retention and the audit line are the pipeline's, not
        this loop's. A run that fails has already written its own failed report — that card
        IS the record — so the loop counts it, spends one of its retries and moves on. A week
        Mojo has not published stores nothing and spends nothing: it is early, not failed.
        Any other exception is swallowed for the same reason: one bad week must not abandon
        the other eleven.
        """
        try:
            report = await self._pipeline.run(trigger=RunTrigger.MANUAL, week=week)
        except asyncio.CancelledError:
            raise  # shutdown is not a week failing
        except WeekNotPublishedError:
            self._deferred += 1
            return
        except Exception as exc:  # noqa: BLE001 — the stored report carries the detail
            print(f"backfill: week {week} failed: {exc}", flush=True)
            self._note_failure(week)
            return
        if report.status != RunStatus.OK:
            self._note_failure(week)
            return
        self._done += 1
        if self._ingestion is not None:
            self._ingestion.clear_week(week)

    def _note_failure(self, week: str) -> None:
        self._failed += 1
        if self._ingestion is not None:
            self._ingestion.record_week_failure(week, datetime.now(UTC))

    def cancel(self) -> None:
        """Stop at the next await. Called from the lifespan's `finally`, and from a worker
        thread by a restore, which is what the maintenance gate's count then waits on.

        A half-finished backfill leaves whole reports behind, never a partial one: the
        pipeline writes each week atomically, and the loop only ever sits between weeks.
        """
        if not self.running():
            return
        try:
            self._task.get_loop().call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            pass  # its loop has closed, and nothing on a closed loop is still running
