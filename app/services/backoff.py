"""Which connections recently failed a best-effort read.

Lived in `app/web/deps.py` until the unattended refreshes needed it too (TV step 15).
Nothing about remembering that a server did not answer is a view concern, and no service
in this app has ever imported the web layer — so it moved down rather than the scheduler
reaching up.
"""

from __future__ import annotations

import threading
import time

# How long a connection that just failed is left alone before a caller bothers it again.
# Shorter than the poster cache's 300s equivalent: a Radarr comes back on a timescale a
# person notices, and the cost of guessing wrong is only that one page renders without a
# library it could have had. Long enough that a dead box costs one timeout a minute
# rather than one per page view.
RETRY_AFTER_SECONDS = 60.0


class RadarrBackoff:
    """Which connections recently failed a best-effort read, so pages stop waiting on them.

    A down Radarr costs the full 4s timeout per connection per render — on the dashboard,
    the weekly view, the search modal and the movie modal alike. The poster cache already
    solves this shape of problem for image hosts (posters.FAILED_RETRY_AFTER_SECONDS);
    this is the same idea for the Radarr reads that merely decorate a page.

    Deliberately NOT consulted by anything whose job is to find out whether a box is back:
    the Settings health dots, Test Connection, and the scheduler's own chart run all still
    really try, every time. A backoff that suppressed those would hide recovery instead of
    surviving an outage.

    Bounded by the number of configured connections, and per app instance rather than
    global, so tests and multiple apps stay isolated.

    Named for Radarr and since asked about Sonarr and the media server too — connection ids
    are unique across kinds, so one map covers all three.
    """

    def __init__(self, retry_after_seconds: float = RETRY_AFTER_SECONDS) -> None:
        self._retry_after = retry_after_seconds
        self._failed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_skip(self, app_id: str) -> bool:
        """True while a recent failure should still be honoured, expiring the entry once
        it is old enough to be worth another attempt."""
        with self._lock:
            failed_at = self._failed_at.get(app_id)
            if failed_at is None:
                return False
            if time.monotonic() - failed_at < self._retry_after:
                return True
            del self._failed_at[app_id]
            return False

    def note_failure(self, app_id: str) -> None:
        with self._lock:
            self._failed_at[app_id] = time.monotonic()

    def note_success(self, app_id: str) -> None:
        """Answered — or its details were just edited, which is a reason to try again now
        rather than after the wait."""
        with self._lock:
            self._failed_at.pop(app_id, None)

    def forget(self, app_id: str) -> None:
        """Drop a removed connection's entry so the map cannot outlive apps.yml."""
        self.note_success(app_id)
