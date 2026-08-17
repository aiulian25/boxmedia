"""Encrypted, complete-state backup and restore (Steps 18–20, ruling #9).

A backup is a snapshot of the ENTIRE `/data` tree except `/data/backups` — config,
history, audit logs, poster cache — so a restore is indistinguishable from the app
never having been lost. The encryption key lives outside `/data` and can never be
swept in. Archives are AES-GCM encrypted (they contain the Radarr API keys and the
admin hash), so they are safe to sync off-box (3-2-1).

Restore is staged: decrypt (GCM authenticates the whole archive) → guard against
path traversal → extract to a staging dir → schema-validate every store → take a
safety backup of the current state → swap directories into place. A failure before
the swap leaves live data untouched. The external-upload path (Step 20) reuses the
exact same validation, since an uploaded file is attacker-controllable.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

import yaml

from app import __version__
from app.core import crypto, filestore
from app.core.audit import AuditAction, AuditLog
from app.services.apps import APPS_FILENAME, APPS_SCHEMA_VERSION
from app.services.ignore import IGNORE_FILENAME, IGNORE_SCHEMA_VERSION
from app.services.radarr_options import (
    RADARR_OPTIONS_FILENAME,
    RADARR_OPTIONS_SCHEMA_VERSION,
)
from app.services.reports import (
    REPORT_FILENAME_PREFIX,
    REPORT_FILENAME_SUFFIX,
    REPORT_SCHEMA_VERSION,
)
from app.services.users import USER_FILENAME, USER_SCHEMA_VERSION

BACKUP_PREFIX = "boxmedia-"
BACKUP_SUFFIX = ".backup"
# The creation time is encoded in the archive's own name; one constant so writing and
# reading it can never disagree.
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_KEEP = 10
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB cap on external restore uploads
CONFIG_SUBDIR = "config"
HISTORY_SUBDIR = "history"
# Top-level data subdirs a backup captures and a restore swaps (never "backups").
_SNAPSHOT_SUBDIRS = (CONFIG_SUBDIR, HISTORY_SUBDIR, "logs", "cache")

# Mirrored from app.services.filters, which imports THIS module — importing it back
# would be a circular import. A test pins these equal to the real ones so they cannot
# drift, the same deliberate mirror app.core.crypto documents for APPS_CONFIG_PATH.
MIRRORED_FILTERS_FILENAME = "filters.yml"
MIRRORED_FILTERS_SCHEMA_VERSION = 1

# Every schema-stamped store, as (path inside the archive, reader, version this build
# can read). Reports are handled separately — there is one file per run, not a fixed name.
_STORE_VERSIONS = (
    (f"{CONFIG_SUBDIR}/{USER_FILENAME}", filestore.read_yaml, USER_SCHEMA_VERSION),
    (f"{CONFIG_SUBDIR}/{APPS_FILENAME}", filestore.read_yaml, APPS_SCHEMA_VERSION),
    (
        f"{CONFIG_SUBDIR}/{MIRRORED_FILTERS_FILENAME}",
        filestore.read_yaml,
        MIRRORED_FILTERS_SCHEMA_VERSION,
    ),
    (f"{CONFIG_SUBDIR}/{IGNORE_FILENAME}", filestore.read_yaml, IGNORE_SCHEMA_VERSION),
    (
        f"{CONFIG_SUBDIR}/{RADARR_OPTIONS_FILENAME}",
        filestore.read_yaml,
        RADARR_OPTIONS_SCHEMA_VERSION,
    ),
)

# What a reader raises when it cannot make sense of a stored document: a schema stamp
# from a newer build, a malformed document, or valid syntax carrying the wrong shape.
# yaml.YAMLError is NOT a ValueError — left out, malformed YAML escapes this check, the
# route's `except BackupError`, and 500s on a tree that was already replaced.
_UNREADABLE_STORE = (filestore.SchemaVersionError, ValueError, yaml.YAMLError)


class BackupError(Exception):
    """A backup or restore operation failed.

    The base of the three specific reasons below, and still raised on its own for the
    failures that have no better name — an archive that is not there, an upload over the
    size cap, a rename that lost a race. Every caller that catches this keeps catching all
    of them, including the scheduler, which must never let a failed backup stop the clock.
    """


class BackupKeyError(BackupError):
    """The archive would not decrypt.

    Named for the usual cause, but AES-GCM authenticates as it decrypts and a failed tag
    check cannot tell a wrong key from altered bytes — the two are the same event. Nothing
    built on this may claim the key alone; the banner says both.
    """


class BackupSchemaError(BackupError):
    """The bytes are intact, and this build does not understand them.

    A newer BoxMedia wrote them. Distinct from corruption because the answer is different:
    upgrade, rather than reach for another archive.
    """


class BackupCorruptError(BackupError):
    """It decrypted, but what came out is not a complete, safe BoxMedia archive.

    A missing manifest, a file the manifest lists that is not there, a checksum that does
    not match, or a member that tries to escape the staging directory.
    """


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size_bytes: int
    created_at: datetime | None = None


def created_at_from_name(name: str) -> datetime | None:
    """The creation time encoded in a backup's name, e.g. boxmedia-20260814-102956-a1b2.

    Read from the name rather than the file's mtime, so copying or syncing an archive
    off-box (3-2-1) doesn't change the moment it reports. None if the name doesn't follow
    the convention.
    """
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    stamped = name[len(BACKUP_PREFIX) : -len(BACKUP_SUFFIX)].split("-")
    if len(stamped) < 2:
        return None
    try:
        moment = datetime.strptime("-".join(stamped[:2]), BACKUP_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC)


class BackupService:
    def __init__(self, data_dir: Path, backups_dir: Path, *, key: bytes, audit: AuditLog) -> None:
        self._data_dir = data_dir
        self._backups_dir = backups_dir
        self._key = key
        self._audit = audit

    # --- Step 18: create / list / prune / delete ---

    def create(self, *, keep: int | None = None, reason: str = "manual") -> str:
        """Take an encrypted snapshot. `keep` prunes to that many archives afterwards.

        `keep=None` means "add without deleting anything". That is the default because
        the retention an admin configured lives in filters.yml, which this service does
        not read — every caller that owns a retention passes it explicitly, and the one
        caller that must never delete (the pre-restore safety net) simply omits it.
        """
        timestamp = datetime.now(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)
        # Random suffix so two backups in the same second (e.g. a manual backup
        # immediately followed by the pre-restore safety backup) never collide.
        name = f"{BACKUP_PREFIX}{timestamp}-{token_hex(2)}{BACKUP_SUFFIX}"
        # Quiesce writes so the archive is a consistent snapshot (flat-file "dump").
        with filestore.write_lock():
            plaintext = self._build_archive()
        blob = crypto.encrypt_bytes(plaintext, self._key)
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        filestore.atomic_write_bytes(self._backups_dir / name, blob)
        if keep is not None:
            self._prune(keep)
        self._audit.record(AuditAction.BACKUP_CREATED, name=name, reason=reason)
        return name

    def _build_archive(self) -> bytes:
        buffer = io.BytesIO()
        checksums: dict[str, str] = {}
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for subdir in _SNAPSHOT_SUBDIRS:
                source = self._data_dir / subdir
                if not source.exists():
                    continue
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    arcname = str(path.relative_to(self._data_dir))
                    data = path.read_bytes()
                    checksums[arcname] = hashlib.sha256(data).hexdigest()
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            manifest = json.dumps(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "app_version": __version__,
                    "created_at": datetime.now(UTC).isoformat(),
                    "files": checksums,
                },
                indent=2,
            ).encode("utf-8")
            manifest_info = tarfile.TarInfo(name=MANIFEST_NAME)
            manifest_info.size = len(manifest)
            tar.addfile(manifest_info, io.BytesIO(manifest))
        return buffer.getvalue()

    def list_backups(self) -> list[BackupInfo]:
        if not self._backups_dir.exists():
            return []
        backups = [
            BackupInfo(
                name=path.name,
                size_bytes=path.stat().st_size,
                created_at=created_at_from_name(path.name),
            )
            for path in self._backups_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
        ]
        backups.sort(key=lambda info: info.name, reverse=True)  # newest first
        return backups

    def _prune(self, keep: int) -> None:
        for stale in self.list_backups()[keep:]:
            (self._backups_dir / stale.name).unlink(missing_ok=True)

    def path_for(self, name: str) -> Path:
        safe_name = Path(name).name
        if not (safe_name.startswith(BACKUP_PREFIX) and safe_name.endswith(BACKUP_SUFFIX)):
            raise BackupError(f"invalid backup name: {name!r}")
        candidate = self._backups_dir / safe_name
        if not candidate.exists():
            raise BackupError(f"backup not found: {safe_name}")
        return candidate

    def delete(self, name: str) -> None:
        self.path_for(name).unlink(missing_ok=True)
        self._audit.record(AuditAction.BACKUP_DELETED, name=Path(name).name)

    # --- Steps 19 & 20: restore ---

    def restore_internal(self, name: str) -> None:
        blob = self.path_for(name).read_bytes()
        self._restore_from_blob(blob, action=AuditAction.BACKUP_RESTORED, source=name)

    def restore_external(self, blob: bytes) -> None:
        if len(blob) > MAX_UPLOAD_BYTES:
            raise BackupError("uploaded backup exceeds the size limit")
        self._restore_from_blob(blob, action=AuditAction.BACKUP_IMPORTED, source="upload")

    def verify(self, name: str) -> None:
        """Answer "would this archive restore?" without restoring it.

        Everything `_restore_from_blob` does up to the point of no return: decrypt, which
        authenticates; extract under the traversal guard; check every checksum against the
        manifest; and ask whether this build can read each store. What it deliberately does
        NOT do is take the pre-restore safety archive or swap anything into place, so
        `/data` is the same afterwards whether this passes or fails — the staging directory
        it writes is its own, and it goes in `finally`.

        Until now the only way to learn an archive was unreadable was to restore it and
        find out, which is a poor moment to discover a wrong key.
        """
        blob = self.path_for(name).read_bytes()
        plaintext = self._decrypt(blob)
        staging = self._staging_dir("verify")
        try:
            self._extract_validated(plaintext, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._audit.record(AuditAction.BACKUP_VERIFIED, source=Path(name).name)

    def _decrypt(self, blob: bytes) -> bytes:
        """Decrypt and authenticate, in one place so verify and restore agree on why."""
        try:
            return crypto.decrypt_bytes(blob, self._key)
        except crypto.DecryptionError as exc:
            raise BackupKeyError(
                "backup is corrupt, tampered, or from a different key"
            ) from exc

    def _staging_dir(self, purpose: str) -> Path:
        """A staging directory of this operation's own.

        Inside `/data` because the runtime's root filesystem is read-only and a rename into
        place has to stay on one filesystem. Named per purpose AND per call: the timestamp
        alone is one-second granularity, so a verify and a restore begun in the same second
        shared a directory, and whichever finished first deleted the other's out from
        under it in its `finally`.
        """
        stamp = f"{datetime.now(UTC):%Y%m%d%H%M%S}-{token_hex(3)}"
        return self._data_dir / f".{purpose}-staging-{stamp}"

    def _restore_from_blob(self, blob: bytes, *, action: str, source: str) -> None:
        plaintext = self._decrypt(blob)
        staging = self._staging_dir("restore")
        try:
            self._extract_validated(plaintext, staging)
            # No `keep`: a safety net taken mid-restore must never delete other
            # archives. The next create that carries a retention prunes it away.
            self.create(reason="pre-restore")
            # Quiesce writers for the swap itself, the same lock `create` holds to read a
            # consistent tree. Without it a scheduled run can write a report into a
            # directory that is renamed away and deleted an instant later, or re-create
            # `live` between the two renames and make the second one fail.
            # `create` has already released the lock, and nothing below re-enters the
            # filestore, so this plain (non-reentrant) lock cannot deadlock here.
            with filestore.write_lock():
                try:
                    self._swap_in(staging)
                except OSError as exc:
                    # A rename that loses a race is an OSError, which the route does not
                    # catch — it would 500 on a half-swapped tree instead of showing the
                    # "nothing was changed" banner.
                    raise BackupError(
                        f"restore failed while swapping data into place: {exc}"
                    ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._audit.record(action, source=Path(source).name)

    def _extract_validated(self, plaintext: bytes, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
            members = tar.getmembers()
            names = {member.name for member in members}
            if MANIFEST_NAME not in names:
                raise BackupCorruptError(
                    "archive is missing its manifest — not a BoxMedia backup"
                )
            # Extract each guarded member by hand — avoids relying on tarfile's `filter`
            # argument, which is absent on older 3.11 patch releases (distroless runtime).
            for member in members:
                self._guard_member(member)
                destination = staging / member.name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                destination.write_bytes(extracted.read() if extracted else b"")
        self._verify_checksums(staging)
        self._verify_schema_versions(staging)

    @staticmethod
    def _guard_member(member: tarfile.TarInfo) -> None:
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise BackupCorruptError(f"archive contains an unsafe path: {name!r}")
        if not (member.isfile() or member.isdir()):
            raise BackupCorruptError(f"archive contains a non-regular entry: {name!r}")

    def _verify_checksums(self, staging: Path) -> None:
        manifest = json.loads((staging / MANIFEST_NAME).read_text(encoding="utf-8"))
        for arcname, expected in manifest.get("files", {}).items():
            extracted = staging / arcname
            if not extracted.exists():
                raise BackupCorruptError(f"archive is missing a listed file: {arcname}")
            actual = hashlib.sha256(extracted.read_bytes()).hexdigest()
            if actual != expected:
                raise BackupCorruptError(
                    f"checksum mismatch for {arcname} — archive is corrupt"
                )

    def _verify_schema_versions(self, staging: Path) -> None:
        """Refuse an archive this build cannot read, BEFORE it replaces live data.

        The checksum pass above proves the bytes survived the round trip; it says nothing
        about whether this build understands them. An archive from a future BoxMedia
        restores cleanly and then raises on every page, with the pre-restore safety
        archive the only way back — so the question is asked while backing out is still
        free.
        """
        for relative, reader, expected in _STORE_VERSIONS:
            candidate = staging / relative
            if not candidate.exists():
                continue
            try:
                reader(candidate, expected_version=expected)
            except _UNREADABLE_STORE as exc:
                raise BackupSchemaError(
                    f"backup contains an unreadable store ({relative}): {exc}"
                ) from exc
        # Sorted so the archive always names the same file first, whatever order the
        # filesystem hands them back.
        reports = sorted(
            (staging / HISTORY_SUBDIR).glob(
                f"{REPORT_FILENAME_PREFIX}*{REPORT_FILENAME_SUFFIX}"
            )
        )
        for report_file in reports:
            try:
                filestore.read_json(report_file, expected_version=REPORT_SCHEMA_VERSION)
            except _UNREADABLE_STORE as exc:
                raise BackupSchemaError(
                    f"backup contains an unreadable report ({report_file.name}): {exc}"
                ) from exc

    def _swap_in(self, staging: Path) -> None:
        """Move each staged subdir into place, retiring the live one it replaces.

        No retired directory is deleted until EVERY rename has succeeded. There is no
        atomic multi-directory rename on POSIX, so a failure partway through is possible
        — and deleting as we went meant the subdirs already swapped had their originals
        destroyed while the restore as a whole had failed. Kept until the end, a failure
        leaves every original sitting in `.<subdir>.retired-*` for recovery.
        """
        retired_dirs: list[Path] = []
        for subdir in _SNAPSHOT_SUBDIRS:
            staged = staging / subdir
            if not staged.exists():
                continue
            live = self._data_dir / subdir
            if live.exists():
                retired = self._data_dir / f".{subdir}.retired-{datetime.now(UTC):%Y%m%d%H%M%S}"
                live.rename(retired)
                retired_dirs.append(retired)
            staged.rename(live)
        # Every subdir is in place; only now is the previous state safe to drop.
        for retired in retired_dirs:
            shutil.rmtree(retired, ignore_errors=True)
