"""Work a page starts but does not wait for: one at a time per key, never lost, never fatal."""

from __future__ import annotations

import asyncio

from app.services.background import SingleFlight


async def test_the_same_work_is_not_started_twice_while_it_runs() -> None:
    background = SingleFlight()
    release = asyncio.Event()
    runs: list[int] = []

    async def refresh() -> None:
        runs.append(1)
        await release.wait()

    assert background.start("refresh", refresh) is True
    assert background.start("refresh", refresh) is False  # two pages, one refresh
    release.set()
    await background.idle()

    assert runs == [1]
    assert background.start("refresh", refresh) is True  # and free again once it is done
    await background.idle()


async def test_a_failure_is_reported_not_raised(capsys) -> None:
    """Nobody is waiting on this work, so an exception has nowhere to go but the log —
    and it must not stop the same work being tried again later."""
    background = SingleFlight()

    async def broken() -> None:
        raise RuntimeError("media server said no")

    background.start("refresh", broken)
    await background.idle()

    assert "background: refresh failed: media server said no" in capsys.readouterr().out
    assert background.start("refresh", broken) is True
    await background.idle()


async def test_shutdown_cancels_what_is_still_running() -> None:
    background = SingleFlight()
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    background.start("posters", forever)
    await started.wait()
    background.cancel_all()
    await background.idle()

    assert background.start("posters", forever) is True  # the cancelled one is done
    background.cancel_all()
