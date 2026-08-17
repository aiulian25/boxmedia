"""F19: the backfill runner — single-flight, polite, and unable to abandon a run."""

from __future__ import annotations

import asyncio

import pytest

from app.services import backfill
from app.services.backfill import (
    BACKFILL_DELAY_SECONDS,
    BACKFILL_JITTER_SECONDS,
    BackfillRunner,
)
from app.services.reports import RunTrigger


class _RecordingPipeline:
    """Stands in for the real pipeline, recording what a backfill asks of it."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.weeks: list[str] = []
        self.triggers: list[str] = []
        self._fail_on = fail_on
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *, trigger: str, week: str | None = None):  # noqa: ANN201
        self.weeks.append(week)
        self.triggers.append(trigger)
        self.started.set()
        if self._fail_on is not None and week == self._fail_on:
            raise RuntimeError("Radarr fell over")
        return None


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause between weeks is the point in production and dead time in a test.

    Patched to a no-op rather than shortened, so a test can never accidentally depend on
    the ordering a real sleep would impose.
    """
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(backfill.asyncio, "sleep", _instant)


async def test_every_week_goes_through_the_pipeline_in_order() -> None:
    pipeline = _RecordingPipeline()
    runner = BackfillRunner(pipeline)

    assert runner.start(["2026W30", "2026W31", "2026W32"]) is True
    await runner._task

    assert pipeline.weeks == ["2026W30", "2026W31", "2026W32"]
    # The ordinary manual path, so dedupe, id reuse, retention and the audit line are all
    # the pipeline's own — this loop adds no second way to write a report.
    assert set(pipeline.triggers) == {RunTrigger.MANUAL}


async def test_a_second_start_while_running_is_refused() -> None:
    """Two of these would double the request rate at Box Office Mojo and race to write the
    same weeks. The second click is almost always the same click."""
    pipeline = _RecordingPipeline()
    pipeline.release.clear()

    async def _blocking(*, trigger: str, week: str | None = None):  # noqa: ANN202
        pipeline.weeks.append(week)
        pipeline.started.set()
        await pipeline.release.wait()

    pipeline.run = _blocking
    runner = BackfillRunner(pipeline)

    assert runner.start(["2026W30", "2026W31"]) is True
    await pipeline.started.wait()
    assert runner.start(["2026W40"]) is False, "single-flight not enforced"
    assert runner.status()["running"] is True

    pipeline.release.set()
    await runner._task
    assert "2026W40" not in pipeline.weeks


async def test_starting_with_nothing_to_do_is_refused() -> None:
    runner = BackfillRunner(_RecordingPipeline())

    assert runner.start([]) is False
    assert runner.status() == {
        "running": False, "done": 0, "total": 0, "current_week": None,
    }


async def test_a_week_that_raises_does_not_abandon_the_rest() -> None:
    """A failed run has already written its own failed report — that card IS the record.
    One bad week must not cost the other eleven."""
    pipeline = _RecordingPipeline(fail_on="2026W31")
    runner = BackfillRunner(pipeline)

    runner.start(["2026W30", "2026W31", "2026W32"])
    await runner._task

    assert pipeline.weeks == ["2026W30", "2026W31", "2026W32"]
    assert runner.status()["done"] == 3
    assert runner.status()["running"] is False


async def test_progress_is_readable_while_it_runs() -> None:
    pipeline = _RecordingPipeline()
    gate = asyncio.Event()

    async def _one_at_a_time(*, trigger: str, week: str | None = None):  # noqa: ANN202
        pipeline.weeks.append(week)
        pipeline.started.set()
        await gate.wait()

    pipeline.run = _one_at_a_time
    runner = BackfillRunner(pipeline)
    runner.start(["2026W30", "2026W31"])
    await pipeline.started.wait()

    status = runner.status()
    assert status["running"] is True
    assert status["total"] == 2
    assert status["current_week"] == "2026W30"
    assert status["done"] == 0  # the first has not landed yet

    gate.set()
    await runner._task
    assert runner.status() == {
        "running": False, "done": 2, "total": 2, "current_week": None,
    }


async def test_cancelling_stops_it_and_clears_the_current_week() -> None:
    """Shutdown must not leave the page claiming a week is in flight."""
    pipeline = _RecordingPipeline()
    never = asyncio.Event()

    async def _hangs(*, trigger: str, week: str | None = None):  # noqa: ANN202
        pipeline.started.set()
        await never.wait()

    pipeline.run = _hangs
    runner = BackfillRunner(pipeline)
    runner.start(["2026W30", "2026W31"])
    await pipeline.started.wait()

    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner._task

    assert runner.status()["running"] is False
    assert runner.status()["current_week"] is None


def test_the_pause_between_weeks_is_long_enough_to_be_polite() -> None:
    """Every week is another page fetched from Box Office Mojo. Twelve weeks therefore
    take three minutes at least, which is the intended shape — a backfill is left to get
    on with, not waited on."""
    assert BACKFILL_DELAY_SECONDS >= 15.0
    assert BACKFILL_JITTER_SECONDS > 0  # not a metronome


async def test_it_waits_between_weeks_but_not_after_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delay is a courtesy to the next request, so there is no next request to be
    courteous to after the final week."""
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(backfill.asyncio, "sleep", _record)
    runner = BackfillRunner(_RecordingPipeline())

    runner.start(["2026W30", "2026W31", "2026W32"])
    await runner._task

    assert len(slept) == 2  # three weeks, two gaps between them
    for pause in slept:
        assert BACKFILL_DELAY_SECONDS <= pause <= BACKFILL_DELAY_SECONDS + BACKFILL_JITTER_SECONDS
    assert len(set(slept)) == len(slept), "the jitter is not varying the pause"
