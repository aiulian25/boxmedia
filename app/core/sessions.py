"""In-memory session store (Step 7).

Sessions live in process memory, not on disk. Two deliberate consequences: a
container restart logs the single admin out (acceptable), and no session state
can ever leak into a backup archive. The session id is a high-entropy random
token carried in an HttpOnly / SameSite=Strict cookie (Secure in production).
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

COOKIE_NAME = "bm_session"
SESSION_ID_BYTES = 32
DEFAULT_TTL = timedelta(hours=12)


@dataclass
class Session:
    username: str
    created_at: datetime
    expires_at: datetime
    # A one-shot payload shown once after sign-in (the "last sign-in" notice). Kept on the
    # session rather than passed through the redirect URL: query strings land in browser
    # history and access logs, and would let a crafted link put arbitrary text on the
    # dashboard. Cleared the first time it is read.
    notice: dict | None = None


class SessionStore:
    def __init__(self, ttl: timedelta = DEFAULT_TTL, idle: timedelta | None = None) -> None:
        self._ttl = ttl
        self._idle = idle
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _sweep_locked(self, now: datetime) -> None:
        """Drop every expired session. Caller must already hold the lock."""
        expired = [key for key, session in self._sessions.items() if session.expires_at <= now]
        for key in expired:
            del self._sessions[key]

    def sweep(self) -> None:
        """Evict expired sessions. Entries are otherwise only dropped when their own id is
        presented again, so a cookie never used again would linger until restart."""
        with self._lock:
            self._sweep_locked(self._now())

    def create(self, username: str, *, notice: dict | None = None) -> str:
        now = self._now()
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        with self._lock:
            self._sweep_locked(now)  # cheap, and login is the natural moment to tidy up
            self._sessions[session_id] = Session(
                username=username, created_at=now, expires_at=now + self._ttl, notice=notice
            )
        return session_id

    def pop_notice(self, session_id: str | None) -> dict | None:
        """Read and clear the session's one-shot notice, so it shows exactly once."""
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.notice is None:
                return None
            notice, session.notice = session.notice, None
            return notice

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now = self._now()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= now:
                del self._sessions[session_id]
                return None
            if self._idle is not None:
                # Sliding idle window, still capped by the absolute TTL: activity extends
                # the session, but never beyond ttl from when it was created.
                session.expires_at = min(session.created_at + self._ttl, now + self._idle)
            return session

    def delete(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def delete_all(self) -> None:
        """Invalidate every session, including the caller's — the post-incident button."""
        with self._lock:
            self._sessions.clear()
