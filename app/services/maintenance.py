"""Keeping a restore whole: nothing runs across it, and an interrupted one is undone.

Two halves. `MaintenanceGate` covers the running process: the filestore lock guards single
writes, so a request or job already holding data it read before a restore would write it
back after one. The journal covers the process dying: POSIX has no atomic multi-directory
rename, so a restore that stops part-way needs a record of how to get back, read at startup
BEFORE anything looks for an account, because a missing config/ would otherwise bootstrap a
brand new admin over the one being restored.

Lock order, for anything joining this: gate first, then the filestore write lock. Neither is
held across an await. The gate's own lock is only ever held to change its counters.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core import filestore

# Long enough for a chart run already under way to finish, short enough to answer inside a
# reverse proxy's usual 60-second read timeout.
DRAIN_TIMEOUT_SECONDS = 45.0

JOURNAL_FILENAME = ".restore-journal.json"
JOURNAL_SCHEMA_VERSION = 1
STATE_ACTIVATING = "activating"
STATE_COMMITTED = "committed"
STAGING_GLOB = ".*-staging-*"
# A journal write cut short leaves its temp file here. Nothing else writes to this directory.
TEMP_FILE_GLOB = ".*.tmp"


class MaintenanceBusyError(Exception):
    """Work already in flight did not finish in time, or a maintenance is already running."""


class RestoreRecoveryError(Exception):
    """An interrupted restore could not be undone. The message says what needs a person."""


class MaintenanceGate:
    """Counts the work in flight, and can close the door on new work until it has drained."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._in_flight = 0
        self._active = False
        self._refusal: str | None = None
        self._stop_hooks: list[Callable[[], None]] = []

    @property
    def refusal(self) -> str | None:
        """Why this process will not serve again, once a rollback has failed."""
        return self._refusal

    def add_stop_hook(self, hook: Callable[[], None]) -> None:
        """Work that would otherwise run long past the drain timeout, cancelled on entry.

        Called from whichever thread began the maintenance.
        """
        self._stop_hooks.append(hook)

    def try_enter(self) -> bool:
        """Count one unit of work in, unless the door is closed. Pair with `leave`."""
        with self._changed:
            if self._active or self._refusal is not None:
                return False
            self._in_flight += 1
            return True

    def leave(self) -> None:
        with self._changed:
            self._in_flight -= 1
            self._changed.notify_all()

    @contextmanager
    def admitted(self) -> Iterator[bool]:
        entered = self.try_enter()
        try:
            yield entered
        finally:
            if entered:
                self.leave()

    @contextmanager
    def exclusive(
        self, *, callers_in_flight: int = 0, timeout: float = DRAIN_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Close the door, stop what can be stopped, and wait for the rest to finish.

        `callers_in_flight` is how much of the count is the caller itself: a request asking
        for this was counted in by the middleware and would otherwise wait for itself.
        Waits on a condition, so call this from a worker thread, never the event loop.
        """
        with self._changed:
            if self._active:
                raise MaintenanceBusyError("another maintenance operation is running")
            self._active = True
        try:
            for hook in self._stop_hooks:
                hook()
            with self._changed:
                drained = self._changed.wait_for(
                    lambda: self._in_flight <= callers_in_flight, timeout=timeout
                )
            if not drained:
                raise MaintenanceBusyError("work in flight did not finish in time")
            yield
        finally:
            with self._changed:
                self._active = False
                self._changed.notify_all()

    def refuse(self, reason: str) -> None:
        """Stop admitting work for the life of this process. A restart retries recovery."""
        with self._changed:
            self._refusal = reason


def _journal_path(data_dir: Path) -> Path:
    return data_dir / JOURNAL_FILENAME


def write_journal(data_dir: Path, *, retired: dict[str, str], state: str) -> None:
    """Durably record a swap and how far it got. `retired` maps live name to retired name.

    `atomic_write_bytes` directly, not `filestore.write_json`: the swap already holds the
    filestore write lock, and that lock is not reentrant.
    """
    document = {
        filestore.SCHEMA_VERSION_KEY: JOURNAL_SCHEMA_VERSION,
        "state": state,
        "retired": retired,
    }
    filestore.atomic_write_bytes(_journal_path(data_dir), json.dumps(document).encode("utf-8"))


def roll_back(data_dir: Path, retired: dict[str, str]) -> None:
    """Put every moved original back, then drop the journal. Safe to repeat.

    A subdir with no retired copy was never moved, so whatever is live is still the original.
    One with a retired copy has its original there and only there, whatever is live now.
    """
    for live_name, retired_name in retired.items():
        original = data_dir / retired_name
        if not original.exists():
            continue
        live = data_dir / live_name
        if live.exists():
            shutil.rmtree(live)
        original.rename(live)
    filestore.fsync_directory(data_dir)
    _journal_path(data_dir).unlink(missing_ok=True)
    filestore.fsync_directory(data_dir)


def finish_commit(data_dir: Path, retired: dict[str, str]) -> None:
    """The restored tree is the state now. Drop the originals, then the journal, so a crash
    in between finds a committed journal and simply finishes this. Safe to repeat."""
    for retired_name in retired.values():
        shutil.rmtree(data_dir / retired_name, ignore_errors=True)
    _journal_path(data_dir).unlink(missing_ok=True)


def recover_interrupted_restore(data_dir: Path) -> None:
    """Finish or undo a restore this process's predecessor did not complete.

    Must run before anything reads or creates data. Raises RestoreRecoveryError when the
    data cannot be made whole, because serving from a half-swapped tree is what this exists
    to prevent. Also clears staging left by a kill, which holds nothing not in an archive.
    """
    path = _journal_path(data_dir)
    if path.exists():
        _recover_from_journal(data_dir, path)
    for staging in data_dir.glob(STAGING_GLOB):
        shutil.rmtree(staging, ignore_errors=True)
    for temp_file in data_dir.glob(TEMP_FILE_GLOB):
        temp_file.unlink(missing_ok=True)


def _recover_from_journal(data_dir: Path, path: Path) -> None:
    try:
        journal = filestore.read_json(path, expected_version=JOURNAL_SCHEMA_VERSION)
        state, retired = journal["state"], journal["retired"]
        if state == STATE_COMMITTED:
            finish_commit(data_dir, retired)
            return
        roll_back(data_dir, retired)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RestoreRecoveryError(recovery_instructions(data_dir, exc)) from exc


def recovery_instructions(data_dir: Path, cause: object) -> str:
    """What a person needs: where things are, and what to do. Named, not summarized."""
    return (
        f"An interrupted restore could not be undone automatically ({cause}). BoxMedia will not "
        f"serve from a half-restored data directory. In {data_dir}, each original is in a "
        f"directory named .<name>.retired-*, and {JOURNAL_FILENAME} records them. Fix the "
        "underlying problem (usually disk space or permissions) and restart, which retries "
        f"this. To recover by hand with BoxMedia stopped: rename each .<name>.retired-* back "
        f"to <name>, replacing what is there, then delete {JOURNAL_FILENAME}."
    )
