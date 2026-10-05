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


class SingleFlight:
    """At most one running task per key, kept referenced until it finishes.

    Single-flight because two pages asking for the same refresh at once want one refresh,
    not two. Referenced because the event loop only keeps a weak reference to a task, and
    one nothing else holds can be collected part-way through.

    Built once per app and held on `app.state`, like the backfill runner: in-process work
    that dies with the process, which the lifespan cancels on the way out.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, key: str, work: Callable[[], Awaitable[None]]) -> bool:
        """Begin `work` unless the same key is already running. True when it started."""
        running = self._tasks.get(key)
        if running is not None and not running.done():
            return False
        self._tasks[key] = asyncio.create_task(self._guarded(key, work))
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
        for task in self._tasks.values():
            task.cancel()
