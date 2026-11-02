"""Atomic flat-file persistence — the data-integrity layer (ruling #1).

With no database, this module is what guarantees a NAS power cut or a mid-write
backup never observes a torn file:

* writes go to a temp file in the same directory, are fsync'd, then atomically
  renamed over the target (rename is atomic within a filesystem);
* every managed document carries a `schema_version` stamp — the hook that lets a
  future format change migrate old files instead of misreading them;
* a single process-wide lock serialises writes so concurrent requests and the
  scheduler can't interleave partial documents.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from stat import S_ISREG
from typing import Any

import yaml

SCHEMA_VERSION_KEY = "schema_version"

# ponytail: one global write lock — trivially correct for ~10 movies/week and one
# admin; swap for per-file locks only if write contention is ever measured.
_write_lock = threading.Lock()


# What a filesystem says when it has no way to sync a directory, which some network mounts
# do not. Nothing can be done about that, and the write itself has already succeeded.
_DIRECTORY_SYNC_UNSUPPORTED = (errno.EINVAL, errno.ENOTSUP, errno.EISDIR)


# Where the running process holds its claim on a data directory.
OWNER_LOCK_FILENAME = ".data-owner.lock"
# The claims this process holds, by data directory, kept open for its whole life: the lock
# lasts exactly as long as the descriptor, so the kernel frees it however the process dies.
_owned_data_dirs: dict[Path, int] = {}


class DataDirInUseError(Exception):
    """Another running process owns this data directory."""


class SchemaVersionError(ValueError):
    """A file's schema_version is newer than this build knows how to read."""


def claim_data_dir(data_dir: Path) -> None:
    """Refuse to share a data directory with another running process.

    Flat files, in-memory sessions and an in-process scheduler all assume one owner, and two
    would overwrite each other without any error at all: a second uvicorn worker, a second
    replica, or a dev stack pointed at production's files. A second call from the same
    process is the same owner.

    Skipped where it cannot work. An unwritable directory is reported better by
    `Settings.ensure_data_dirs`, and some network mounts have no flock; neither is a reason
    to stop an install that has only one process anyway.
    """
    resolved = data_dir.resolve()
    if resolved in _owned_data_dirs:
        return
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(resolved / OWNER_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise DataDirInUseError(
            f"another BoxMedia process is using {resolved}. BoxMedia runs as exactly one "
            "process per data directory: run a single uvicorn worker and a single replica, "
            "and give a second stack its own data directory."
        ) from exc
    except OSError:
        os.close(descriptor)
        return
    _owned_data_dirs[resolved] = descriptor


def fsync_directory(path: Path) -> None:
    """Make a rename inside `path` survive a power cut. Syncing the file is not enough: the
    name that points at it lives in the directory."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _DIRECTORY_SYNC_UNSUPPORTED:
            raise
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes via temp file + fsync + atomic rename + directory fsync (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        fsync_directory(path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _stamp(data: dict[str, Any], schema_version: int) -> dict[str, Any]:
    stamped = dict(data)
    stamped[SCHEMA_VERSION_KEY] = schema_version
    return stamped


def _check_version(data: dict[str, Any], expected_version: int, source: Path) -> None:
    found = data.get(SCHEMA_VERSION_KEY)
    if found is not None and isinstance(found, int) and found > expected_version:
        raise SchemaVersionError(
            f"{source} has schema_version {found}, but this build supports "
            f"up to {expected_version}"
        )


def write_yaml(path: Path, data: dict[str, Any], *, schema_version: int) -> None:
    stamped = _stamp(data, schema_version)
    payload = yaml.safe_dump(stamped, sort_keys=False, allow_unicode=True).encode("utf-8")
    with _write_lock:
        atomic_write_bytes(path, payload)


def write_json(path: Path, data: dict[str, Any], *, schema_version: int) -> None:
    stamped = _stamp(data, schema_version)
    payload = json.dumps(stamped, indent=2, ensure_ascii=False).encode("utf-8")
    with _write_lock:
        atomic_write_bytes(path, payload)


def read_yaml(path: Path, *, expected_version: int) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a YAML mapping")
    _check_version(data, expected_version, path)
    return data


def read_json(path: Path, *, expected_version: int) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    _check_version(data, expected_version, path)
    return data


def write_lock() -> threading.Lock:
    """The shared write lock, exposed so backup/restore can quiesce writes."""
    return _write_lock


def dir_size_bytes(path: Path) -> int:
    """Total bytes of the regular files directly in `path` (0 when it doesn't exist).

    Flat by design: every /data subdirectory BoxMedia reports on is flat, and a recursive
    walk would follow whatever a symlink points at.

    Tolerant of concurrent deletion at both levels, because the Settings Storage card
    walks these directories on every render while the scheduled backup prunes them and a
    restore renames them wholesale. Whatever vanishes mid-walk contributes nothing, which
    is the honest number — it is no longer occupying anything. Listing eagerly separates
    "the directory went away" from "one file went away", so the second cannot abandon the
    rest of the walk.
    """
    try:
        entries = list(path.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return 0
    total = 0
    for entry in entries:
        try:
            info = entry.stat()
        except FileNotFoundError:
            continue  # pruned since the listing — it costs nothing now
        # One stat, asked once: `is_file()` followed by `stat()` asks the kernel the same
        # question twice and leaves a window between the two answers.
        if S_ISREG(info.st_mode):
            total += info.st_size
    return total
