"""Unattended re-reads of the servers you connected (TV step 15).

Two callers need exactly the same work done, and only one of them has a request to hang
it off: the calendar page, when its cache has gone stale, and the morning job, which runs
with nobody watching. So the work lives here, in the service layer, and both callers ask
this object — rather than the scheduler reaching up into `app.web`, which no service in
this app has ever done.

Everything here is **best-effort and bounded**, the same contract every page read already
has. A server that does not answer costs one timeout, is noted in the backoff so the next
caller does not pay it again, and leaves the previous answer in place: "we could not look"
and "there is nothing" are different claims, and only one of them is true.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.config import Settings
from app.services.apps import KIND_SONARR, AppsStore, ExternalApp, InvalidAppError
from app.services.backoff import RadarrBackoff
from app.services.calendar import (
    CalendarCache,
    CalendarEntry,
    entry_from_episode,
    entry_from_release,
    window_for,
)
from app.services.radarr import RadarrClient, RadarrError
from app.services.series import SeriesLibraryCache
from app.services.sonarr import SonarrClient, SonarrError

# The calendar is a page's whole content rather than a decoration, so it is worth a little
# more patience than a library probe — but still a hard ceiling, because a page that hangs
# on somebody's NAS is a page nobody can close.
CALENDAR_TIMEOUT_SECONDS = 6.0
# What a library read gets. The same 4s every other best-effort library read already uses.
LIBRARY_TIMEOUT_SECONDS = 4.0


class ServerRefresher:
    """Re-reads the user's own Radarr and Sonarr connections into the local caches.

    Built once at composition and held on `app.state`, the way `Pipeline` is: it owns the
    policies that make an unattended read safe — which client, how long to wait, what to
    keep when a box is silent — rather than passing values between two layers.
    """

    def __init__(
        self,
        *,
        apps: AppsStore,
        settings: Settings,
        calendar_cache: CalendarCache,
        series_cache: SeriesLibraryCache,
        backoff: RadarrBackoff,
    ) -> None:
        self._apps = apps
        self._settings = settings
        self._calendar_cache = calendar_cache
        self._series_cache = series_cache
        self._backoff = backoff

    # --- the calendar ---

    async def calendar(self) -> bool:
        """Re-read every connection's window into the merged cache. True when all answered.

        A connection that does not answer keeps the rows it contributed last time rather
        than vanishing from the week, and the cache records that, so every later view can
        say so and not only this request.
        """
        now = datetime.now(UTC)
        start, end = window_for(now.date())
        apps = self._apps.list_apps(kind=None)
        # Every connection at once: one slow server costs the timeout, never the sum.
        results = await asyncio.gather(
            *[self._read_calendar(app, start=start, end=end, now=now) for app in apps]
        )
        answered = {
            app.id for app, rows in zip(apps, results, strict=True) if rows is not None
        }
        fresh = [entry for rows in results if rows is not None for entry in rows]
        await asyncio.to_thread(
            self._calendar_cache.merge,
            fresh,
            answered=answered,
            unanswered={app.id for app in apps} - answered,
            legacy_names={app.name: app.id for app in apps},
            now=now.timestamp(),
        )
        return all(rows is not None for rows in results)

    async def _read_calendar(
        self, app: ExternalApp, *, start: datetime, end: datetime, now: datetime
    ) -> list[CalendarEntry] | None:
        """One connection's window as entries, or None when it could not be read.

        The two kinds differ in four places — which client, which error, which reader, and
        which id the queue is keyed by — and in nothing else, so they share one body.
        """
        if self._backoff.should_skip(app.id):
            return None
        is_series = app.kind == KIND_SONARR
        build = self._sonarr_client if is_series else self._radarr_client
        try:
            client = build(app.id, timeout=CALENDAR_TIMEOUT_SECONDS)
            rows, progress = await asyncio.gather(
                asyncio.wait_for(
                    client.calendar(start, end), timeout=CALENDAR_TIMEOUT_SECONDS
                ),
                _queue_or_empty(client),
            )
        except (RadarrError, SonarrError, TimeoutError, KeyError, InvalidAppError):
            self._backoff.note_failure(app.id)
            return None
        self._backoff.note_success(app.id)
        if not is_series:
            return [
                entry_from_release(
                    row, connection=app.id, progress=progress.get(row.radarr_id), now=now
                )
                for row in rows
            ]
        episodes = [
            entry_from_episode(
                row, connection=app.id, progress=progress.get(row.series_id), now=now
            )
            for row in rows
        ]
        return [entry for entry in episodes if entry is not None]

    # --- what each Sonarr holds ---

    async def series(self) -> bool:
        """Re-read every Sonarr's library into the snapshot. True when all answered.

        Every connection rather than only the stale ones: this is the unattended top-up,
        and a snapshot that is merely nearly-stale in the morning is stale by lunchtime.
        """
        return await self._refresh_series(self._apps.list_apps(KIND_SONARR))

    async def stale_series_libraries(self) -> bool:
        """The same, for whichever snapshots have aged out — what a page open pays for.

        A connection already inside its TTL is not asked, and one that just failed is not
        asked either: on the page path the answer only decorates what is already rendering.
        """
        stale = [
            app
            for app in self._apps.list_apps(KIND_SONARR)
            if self._series_cache.is_stale(app.id) and not self._backoff.should_skip(app.id)
        ]
        return await self._refresh_series(stale)

    async def series_library(self, app_id: str) -> bool:
        """One Sonarr, now — what an Add asks for so the card is right immediately."""
        try:
            client = self._sonarr_client(app_id, timeout=LIBRARY_TIMEOUT_SECONDS)
            series = await asyncio.wait_for(
                client.list_series(), timeout=LIBRARY_TIMEOUT_SECONDS
            )
        except (SonarrError, TimeoutError, KeyError, InvalidAppError):
            self._backoff.note_failure(app_id)
            return False
        self._backoff.note_success(app_id)
        await asyncio.to_thread(self._series_cache.save, app_id, tuple(series))
        return True

    async def _refresh_series(self, apps: list[ExternalApp]) -> bool:
        if not apps:
            return True
        answers = await asyncio.gather(
            *[self.series_library(app.id) for app in apps]
        )
        return all(answers)

    # --- one place for the outbound-TLS dance ---

    def _radarr_client(self, app_id: str, *, timeout: float) -> RadarrClient:
        return self._apps.build_client(app_id, timeout=timeout, **self._settings.outbound_tls())

    def _sonarr_client(self, app_id: str, *, timeout: float) -> SonarrClient:
        return self._apps.build_sonarr_client(
            app_id, timeout=timeout, **self._settings.outbound_tls()
        )


async def _queue_or_empty(client: RadarrClient | SonarrClient) -> dict[int, float]:
    """How far along each download is, or nothing.

    The queue decorates the calendar; it does not define it. A server that answers its
    calendar but not its queue should still fill the week, with no progress bars.
    """
    try:
        return await asyncio.wait_for(client.queue(), timeout=CALENDAR_TIMEOUT_SECONDS)
    except (RadarrError, SonarrError, TimeoutError, KeyError):
        return {}
