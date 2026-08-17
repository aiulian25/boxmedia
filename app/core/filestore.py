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


class SchemaVersionError(ValueError):
    """A file's schema_version is newer than this build knows how to read."""


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes via temp file + fsync + atomic rename (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
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
