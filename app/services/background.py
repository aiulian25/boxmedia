"""Work a page starts but does not wait for.

A page used to sit on every slow thing it touched: the media server's full library re-read
once its fifteen minutes were up (1.3s for 2,991 films), and every poster it had not shown
before (up to 1.2s on a first visit). Neither changes what the page can truthfully say right
now — a stale snapshot still answers, and a poster not yet cached has a placeholder — so both
now happen after the response, here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.services.maintenance import MaintenanceGate


class SingleFlight:
    """At most one running task per key, kept referenced until it finishes.

    Single-flight because two pages asking for the same refresh at once want one refresh,
    not two. Referenced because the event loop only keeps a weak reference to a task, and
    one nothing else holds can be collected part-way through.

    Built once per app and held on `app.state`, like the backfill runner: in-process work
    that dies with the process, which the lifespan cancels on the way out.
    """

    def __init__(self, maintenance: MaintenanceGate | None = None) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._maintenance = maintenance or MaintenanceGate()

    def start(self, key: str, work: Callable[[], Awaitable[None]]) -> bool:
        """Begin `work` unless the same key is running or a maintenance has the door closed.
        True when it started."""
        running = self._tasks.get(key)
        if running is not None and not running.done():
            return False
        if not self._maintenance.try_enter():
            return False
        task = asyncio.create_task(self._guarded(key, work))
        # A callback rather than a `finally`: a task cancelled before its first step never
        # enters the coroutine, and the gate would wait for it for ever.
        task.add_done_callback(lambda _: self._maintenance.leave())
        self._tasks[key] = task
        return True

    async def _guarded(self, key: str, work: Callable[[], Awaitable[None]]) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            raise  # shutdown is not a failure
        except Exception as exc:  # noqa: BLE001 — nobody is waiting to be told; say it here
            print(f"background: {key} failed: {exc}", flush=True)

    async def idle(self) -> None:
        """Return once nothing is running — including anything started while waiting."""
        while running := [task for task in self._tasks.values() if not task.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    def cancel_all(self) -> None:
        """Request cancellation, from any thread: a restore calls this from a worker. That
        it has finished is what the maintenance gate's count says, not this."""
        for task in self._tasks.values():
            if task.done():
                continue
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                continue  # its loop has closed, and nothing on a closed loop is still running
