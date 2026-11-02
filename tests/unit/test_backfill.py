"""F19: the backfill runner — single-flight, polite, and unable to abandon a run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import backfill
from app.services.backfill import (
    BACKFILL_DELAY_SECONDS,
    BACKFILL_JITTER_SECONDS,
    BackfillRunner,
)
from app.services.boxoffice import WeekNotPublishedError
from app.services.ingestion_state import IngestionStateStore
from app.services.reports import RunStatus, RunTrigger


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
        return SimpleNamespace(status=RunStatus.OK)


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
        return SimpleNamespace(status=RunStatus.OK)

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
        "running": False, "done": 0, "failed": 0, "deferred": 0, "total": 0,
        "current_week": None,
    }


async def test_a_week_that_raises_does_not_abandon_the_rest() -> None:
    """A failed run has already written its own failed report — that card IS the record.
    One bad week must not cost the other eleven."""
    pipeline = _RecordingPipeline(fail_on="2026W31")
    runner = BackfillRunner(pipeline)

    runner.start(["2026W30", "2026W31", "2026W32"])
    await runner._task

    assert pipeline.weeks == ["2026W30", "2026W31", "2026W32"]
    assert runner.status()["done"] == 2
    assert runner.status()["failed"] == 1
    assert runner.status()["running"] is False


async def test_progress_is_readable_while_it_runs() -> None:
    pipeline = _RecordingPipeline()
    gate = asyncio.Event()

    async def _one_at_a_time(*, trigger: str, week: str | None = None):  # noqa: ANN202
        pipeline.weeks.append(week)
        pipeline.started.set()
        await gate.wait()
        return SimpleNamespace(status=RunStatus.OK)

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
        "running": False, "done": 2, "failed": 0, "deferred": 0, "total": 2,
        "current_week": None,
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


class _AnsweringPipeline:
    """Each week gets the answer it was given: a status, or an exception to raise."""

    def __init__(self, answers: dict[str, object]) -> None:
        self._answers = answers

    async def run(self, *, trigger: str, week: str | None = None):  # noqa: ANN201, ARG002
        answer = self._answers[week]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(status=answer)


async def test_it_counts_what_each_week_actually_came_to(tmp_path) -> None:  # noqa: ANN001
    """It used to count every week as fetched, so an outage reported a full house."""
    ingestion = IngestionStateStore(tmp_path)
    runner = BackfillRunner(_AnsweringPipeline({
        "2026W30": RunStatus.OK,
        "2026W31": RunStatus.RADARR_FAILED,
        "2026W32": WeekNotPublishedError("not yet"),
        "2026W33": RuntimeError("a bug"),
    }), ingestion=ingestion)

    runner.start(["2026W30", "2026W31", "2026W32", "2026W33"])
    await runner._task

    status = runner.status()
    assert (status["done"], status["failed"], status["deferred"]) == (1, 2, 1)
    assert set(ingestion.week_retries()) == {"2026W31", "2026W33"}  # early is not failed


async def test_a_week_that_comes_good_forgets_its_attempts(tmp_path) -> None:  # noqa: ANN001
    ingestion = IngestionStateStore(tmp_path)
    runner = BackfillRunner(
        _AnsweringPipeline({"2026W31": RunStatus.RADARR_FAILED}), ingestion=ingestion
    )
    runner.start(["2026W31"])
    await runner._task
    assert ingestion.week_retries()["2026W31"].attempts == 1

    recovered = BackfillRunner(_AnsweringPipeline({"2026W31": RunStatus.OK}), ingestion=ingestion)
    recovered.start(["2026W31"])
    await recovered._task

    assert ingestion.week_retries() == {}
