"""Append-only JSONL audit log (Step 5).

Built before auth on purpose: for a single-account credential broker exposed
through a reverse proxy, the record of who logged in (and who failed) is the
admin's only signal that they are under attack. Every security-relevant event —
logins, password changes, config edits, backup/restore — lands here as one JSON
object per line, with newlines inside fields escaped by the encoder so a crafted
value can't forge a second log entry.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

AUDIT_FILENAME = "audit.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB
DEFAULT_KEEP = 3  # audit.jsonl + .1 + .2
DEFAULT_TAIL_LIMIT = 200
# Read only this much from the end of the log: 200 entries are a few tens of KB, so a
# page view costs a bounded read no matter how close the file is to its rotation size.
TAIL_READ_BYTES = 256 * 1024
# `actor` is whatever was typed into the login form, so it arrives from an
# UNAUTHENTICATED request. Rotation caps total size, which means an unbounded value does
# not fill the disk — it does something worse, rotating the real security history out of
# existence. Clipped at the storage boundary, where this module already takes
# responsibility for hostile field values (see the newline note above).
MAX_ACTOR_LENGTH = 120


class AuditAction:
    """Canonical action names (no magic strings scattered across modules)."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGIN_LOCKED = "login_locked"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 — audit action name, not a secret
    PASSWORD_CHANGE_REJECTED = "password_change_rejected"  # noqa: S105 — action name
    SESSIONS_CLEARED = "sessions_cleared"
    PROFILE_UPDATED = "profile_updated"
    ADMIN_BOOTSTRAPPED = "admin_bootstrapped"
    APP_ADDED = "app_added"
    APP_UPDATED = "app_updated"
    APP_REMOVED = "app_removed"
    APP_TESTED = "app_tested"
    MEDIA_SERVER_UPDATED = "media_server_updated"
    MEDIA_SERVER_REMOVED = "media_server_removed"
    MEDIA_SERVER_TESTED = "media_server_tested"
    FILTERS_UPDATED = "filters_updated"
    PIPELINE_RUN = "pipeline_run"
    MOVIE_ADDED_MANUAL = "movie_added_manual"
    MATCH_CORRECTED = "match_corrected"
    REPORT_DELETED = "report_deleted"
    BACKUP_CREATED = "backup_created"
    BACKUP_RESTORED = "backup_restored"
    BACKUP_IMPORTED = "backup_imported"
    BACKUP_DELETED = "backup_deleted"
    BACKUP_VERIFIED = "backup_verified"
    BACKUP_FAILED = "backup_failed"
    MAINTENANCE_PRUNE = "maintenance_prune"


def _clip_actor(actor: str | None) -> str | None:
    """Bound the one field an unauthenticated request controls.

    Marked with an ellipsis so a reader can tell the value was cut rather than being
    that short. No real username reaches this length — the profile form caps it far
    lower — so only a crafted login gets clipped.
    """
    if actor is None or len(actor) <= MAX_ACTOR_LENGTH:
        return actor
    return f"{actor[:MAX_ACTOR_LENGTH]}…"


class AuditLog:
    """Thread-safe append-only JSONL writer with size-based rotation."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._keep = keep
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        *,
        actor: str | None = None,
        source_ip: str | None = None,
        **details: object,
    ) -> None:
        entry: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "action": action,
            "actor": _clip_actor(actor),
            "source_ip": source_ip,
        }
        entry.update(details)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as handle:
                handle.write(encoded)

    def tail(self, limit: int = DEFAULT_TAIL_LIMIT) -> list[dict]:
        """The most recent entries, newest first — the read side of this log.

        Only the live `audit.jsonl` is searched; rotated files (`.1`, `.2`) are out of
        scope. A torn last line (crash mid-write) is skipped rather than failing the page.
        """
        if not self._path.exists():
            return []
        with self._path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - TAIL_READ_BYTES)
            handle.seek(start)
            raw = handle.read()

        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            del lines[0]  # the byte offset almost certainly cut this line in half

        entries: list[dict] = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        entries.reverse()
        return entries

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if not self._path.exists():
            return
        if self._path.stat().st_size + incoming_bytes <= self._max_bytes:
            return
        # audit.jsonl -> .1, .1 -> .2, dropping the oldest beyond `keep`.
        oldest = self._path.with_suffix(self._path.suffix + f".{self._keep - 1}")
        oldest.unlink(missing_ok=True)
        for index in range(self._keep - 2, 0, -1):
            src = self._path.with_suffix(self._path.suffix + f".{index}")
            if src.exists():
                src.rename(self._path.with_suffix(self._path.suffix + f".{index + 1}"))
        self._path.rename(self._path.with_suffix(self._path.suffix + ".1"))


@lru_cache
def get_audit_log() -> AuditLog:
    from app.core.config import get_settings

    return AuditLog(get_settings().logs_dir / AUDIT_FILENAME)
