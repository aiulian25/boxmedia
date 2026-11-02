"""Encrypted, complete-state backup and restore (Steps 18–20, ruling #9).

A backup is a snapshot of the ENTIRE `/data` tree except `/data/backups` — config,
history, audit logs, poster cache — so a restore is indistinguishable from the app
never having been lost. The encryption key lives outside `/data` and can never be
swept in. Archives are AES-GCM encrypted (they contain the Radarr API keys and the
admin hash), so they are safe to sync off-box (3-2-1).

Restore is staged: decrypt (GCM authenticates the whole archive) → check the members and
their budgets → extract to a staging dir → check the manifest and its inventory → load every
store through the code the app runs on → take a safety backup of the current state → swap
directories into place under a journal. A failure before the swap leaves live data untouched,
one during it is rolled back, and a crash during it is undone at the next start. Nothing else
runs across the safety backup and the swap (see app.services.maintenance). The external-upload
path (Step 20) reuses the exact same validation, since an uploaded file is
attacker-controllable.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from itertools import chain
from pathlib import Path, PurePosixPath
from secrets import token_hex
from typing import BinaryIO

from app import __version__
from app.core import crypto, filestore
from app.core.audit import AuditAction, AuditLog
from app.services import maintenance
from app.services.apps import APPS_FILENAME, AppsStore
from app.services.corrections import CORRECTIONS_FILENAME, CorrectionStore
from app.services.discovery import DISCOVERY_FILENAME, DiscoveryStore
from app.services.ignore import IGNORE_FILENAME, IgnoreStore
from app.services.ingestion_state import INGESTION_FILENAME, IngestionStateStore
from app.services.maintenance import MaintenanceBusyError, MaintenanceGate
from app.services.mediaserver import (
    LEGACY_PLEX_FILENAME,
    MEDIA_SERVER_FILENAME,
    MediaServerStore,
)
from app.services.radarr_options import RADARR_OPTIONS_FILENAME, RadarrOptionsCache
from app.services.reports import REPORT_FILENAME_PREFIX, REPORT_FILENAME_SUFFIX, ReportsStore
from app.services.sonarr_options import SONARR_OPTIONS_FILENAME, SonarrOptionsCache
from app.services.users import USER_FILENAME, UserStore

BACKUP_PREFIX = "boxmedia-"
BACKUP_SUFFIX = ".backup"
# The creation time is encoded in the archive's own name; one constant so writing and
# reading it can never disagree.
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_KEEP = 10
# What an archive may expand to. Far above what a real install holds (433 files, 17 MiB, the
# largest 360 KiB, when these were set), so they stop a damaged or foreign archive without
# refusing a genuine one. Archives stream, so memory is not what limits them.
MAX_ARCHIVE_MEMBERS = 20_000  # each costs a TarInfo in memory
MAX_MEMBER_BYTES = 64 * 1024 * 1024  # posters and the audit log both stop at 5 MiB
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024  # staging disk, not memory: members are streamed
# An upload is copied to disk before anything reads it, and an archive is no bigger than what
# it holds in any way that matters here, so it shares the same limit.
MAX_UPLOAD_BYTES = MAX_EXPANDED_BYTES
UPLOAD_COPY_BYTES = 1024 * 1024
PARTIAL_SUFFIX = ".partial"
LEGACY_PLAINTEXT_NAME = ".legacy-plaintext"
# Unlike a streamed member, a store is parsed whole, and PyYAML builds far more than the file
# weighs: measured in the image, 1 MiB peaks at 127 MiB and 4 MiB is killed at the 256 MiB
# limit. The app parses these on every page too, so a larger one could not run anyway.
MAX_STORE_BYTES = 1024 * 1024
# JSON, so it costs a fraction of that, and it has one line per member: 20,000 fit in 4 MiB.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# Written data counts against the container's memory limit until it reaches the disk, and the
# kernel kills a process that outruns writeback: 2 GiB staged unsynced was killed at 256 MiB
# with the process itself at 68 MiB.
STAGING_SYNC_BYTES = 32 * 1024 * 1024
CONFIG_SUBDIR = "config"
HISTORY_SUBDIR = "history"
LOGS_SUBDIR = "logs"
CACHE_SUBDIR = "cache"
# Top-level data subdirs a backup captures and a restore swaps (never "backups").
_SNAPSHOT_SUBDIRS = (CONFIG_SUBDIR, HISTORY_SUBDIR, LOGS_SUBDIR, CACHE_SUBDIR)
# The subset a restore has to see as a single moment, and so the only part read with
# writers quiesced. The poster cache is derived and content-addressed: a poster
# written or pruned mid-read cannot make the archive disagree with itself. It is also
# the one subdir with no ceiling, so holding the lock across it stalls every write in
# the process — a scheduled run included — for as long as reading it takes.
_CONSISTENT_SUBDIRS = (CONFIG_SUBDIR, HISTORY_SUBDIR, LOGS_SUBDIR)
# Derived rather than listed, so a subdir added to the snapshot above cannot be
# silently left out of one.
_STREAMED_SUBDIRS = tuple(
    subdir for subdir in _SNAPSHOT_SUBDIRS if subdir not in _CONSISTENT_SUBDIRS
)

# Mirrored from app.services.filters, which imports THIS module — importing it back at the
# top would be a circular import. A test pins this equal to the real one so it cannot drift,
# the same deliberate mirror app.core.crypto documents for APPS_CONFIG_PATH.
MIRRORED_FILTERS_FILENAME = "filters.yml"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# What a damaged archive raises on its way out of the staging step. OSError is deliberately
# absent, apart from the three that mean two members claim one path: a full disk is not a
# damaged archive, and saying so would send someone to the wrong remedy.
_MALFORMED_ARCHIVE = (
    tarfile.TarError,
    EOFError,
    zlib.error,
    gzip.BadGzipFile,
    NotADirectoryError,
    FileExistsError,
    IsADirectoryError,
    ValueError,
)


def _load_filters(config_dir: Path, key: bytes, audit: AuditLog) -> object:
    from app.services.filters import FiltersStore  # see MIRRORED_FILTERS_FILENAME

    return FiltersStore(config_dir, audit=audit).load()


def _load_media_server(config_dir: Path, key: bytes, audit: AuditLog) -> object:
    return MediaServerStore(config_dir, key=key, audit=audit).load()


# Every durable store in config/, each loaded by the code the running app loads it with, so
# preflight and runtime cannot disagree about what is usable. A stored credential is checked
# for shape and not decrypted: the app runs on with one it cannot read and asks for it again
# (README, "Rotating the encryption key"), so refusing here would lock a whole history behind
# one credential. Caches and logs are absent on purpose: their readers tolerate anything.
_STORE_LOADERS: tuple[tuple[str, Callable[[Path, bytes, AuditLog], object]], ...] = (
    (USER_FILENAME, lambda config, _, audit: UserStore(config, audit=audit).validate_stored()),
    (APPS_FILENAME, lambda config, key, audit: AppsStore(config, key=key, audit=audit).list_apps(
        kind=None
    )),
    (MIRRORED_FILTERS_FILENAME, _load_filters),
    (IGNORE_FILENAME, lambda config, _, audit: IgnoreStore(config, audit=audit).list_ignored()),
    (CORRECTIONS_FILENAME, lambda config, _, __: CorrectionStore(config).book()),
    (INGESTION_FILENAME, lambda config, _, __: IngestionStateStore(config).read()),
    (RADARR_OPTIONS_FILENAME, lambda config, _, __: RadarrOptionsCache(config).load_all()),
    (SONARR_OPTIONS_FILENAME, lambda config, _, __: SonarrOptionsCache(config).load_all()),
    (MEDIA_SERVER_FILENAME, _load_media_server),
    (LEGACY_PLEX_FILENAME, _load_media_server),
    (DISCOVERY_FILENAME, lambda config, key, audit: DiscoveryStore(
        config, key=key, audit=audit
    ).load()),
)


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


class BackupBusyError(BackupError):
    """Work already running did not finish in time, so the restore never began. Try again."""


class BackupRollbackError(BackupError):
    """A swap failed and so did undoing it. This process has stopped serving; see the gate."""


class BackupCorruptError(BackupError):
    """It decrypted, but what came out is not a complete, safe, usable BoxMedia archive.

    A missing or malformed manifest, a file list that does not match the members, a checksum
    that does not match, a member that tries to escape the staging directory, an archive past
    its budgets, or a store the app could not run on. The last can be intact bytes: an archive
    of an account nobody could log in to is as useless as a damaged one.
    """


class _ExpansionBudget:
    """What an archive may expand to, in one place so writing one and reading one agree. An
    archive `create` could write and `verify` would refuse is one found out too late."""

    def __init__(self) -> None:
        self._members = 0
        self._bytes = 0

    def admit(self, name: str, size: int) -> None:
        self._members += 1
        self._bytes += size
        if self._members > MAX_ARCHIVE_MEMBERS:
            raise BackupCorruptError(f"archive has over {MAX_ARCHIVE_MEMBERS} members")
        if size > MAX_MEMBER_BYTES:
            raise BackupCorruptError(f"archive member is over the size limit: {name}")
        if self._bytes > MAX_EXPANDED_BYTES:
            raise BackupCorruptError(f"archive expands past {MAX_EXPANDED_BYTES} bytes")


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


class _SyncedSink:
    """Pushes an archive to the disk as it is written. Dirty pages count against the container's
    memory limit until they land, for the reason STAGING_SYNC_BYTES gives."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._unsynced = 0

    def write(self, data: bytes) -> int:
        self._handle.write(data)
        self._unsynced += len(data)
        if self._unsynced >= STAGING_SYNC_BYTES:
            self.sync()
        return len(data)

    def sync(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._unsynced = 0


class BackupService:
    def __init__(
        self,
        data_dir: Path,
        backups_dir: Path,
        *,
        key: bytes,
        audit: AuditLog,
        maintenance: MaintenanceGate | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._backups_dir = backups_dir
        self._key = key
        self._audit = audit
        self._maintenance = maintenance or MaintenanceGate()
        # One process owns this directory, so what is partial at startup was killed part-way.
        if backups_dir.exists():
            for stale in backups_dir.glob(f".*{PARTIAL_SUFFIX}"):
                stale.unlink(missing_ok=True)

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
        # Quiesce writes only across the stores that have to agree with each other.
        # The poster cache and every byte of compression follow unlocked — they are
        # the slow part, and neither needs writers held still.
        with filestore.write_lock():
            consistent = self._collect(_CONSISTENT_SUBDIRS)
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        partial_path = self._backups_dir / f".{name}{PARTIAL_SUFFIX}"
        try:
            with partial_path.open("wb") as handle:
                sink = _SyncedSink(handle)
                writer = crypto.ChunkedWriter(sink, self._key)
                self._pack(chain(consistent, self._read_files(_STREAMED_SUBDIRS)), writer)
                writer.finish()
                sink.sync()
            partial_path.replace(self._backups_dir / name)
            filestore.fsync_directory(self._backups_dir)
        except BaseException:
            partial_path.unlink(missing_ok=True)
            raise
        if keep is not None:
            self._prune(keep)
        self._audit.record(AuditAction.BACKUP_CREATED, name=name, reason=reason)
        return name

    def _read_files(self, subdirs: tuple[str, ...]) -> Iterator[tuple[str, bytes]]:
        """Yield (archive name, bytes) for each file under `subdirs`, one at a time."""
        for subdir in subdirs:
            source = self._data_dir / subdir
            if not source.exists():
                continue
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                yield str(path.relative_to(self._data_dir)), path.read_bytes()

    def _collect(self, subdirs: tuple[str, ...]) -> list[tuple[str, bytes]]:
        """Read `subdirs` eagerly. Being eager is the entire point: the caller holds
        the write lock, and these files have to be off disk before it lets go.

        Only ever called for the bounded stores — config, the retained reports and the
        rotated audit logs. The poster cache has no ceiling, so it is streamed through
        `_read_files` instead of being held in memory alongside the archive.
        """
        return list(self._read_files(subdirs))

    def _pack(self, files: Iterable[tuple[str, bytes]], output: object) -> None:
        """Compress (archive name, bytes) pairs into the manifest-bearing tar, as a stream."""
        checksums: dict[str, str] = {}
        budget = _ExpansionBudget()
        with tarfile.open(fileobj=output, mode="w|gz") as tar:
            for arcname, data in files:
                budget.admit(arcname, len(data))
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

    def restore_internal(
        self,
        name: str,
        *,
        callers_in_flight: int = 0,
        on_activated: Callable[[], None] | None = None,
    ) -> None:
        """See `_restore_from_file` for what the two keywords mean."""
        self._restore_from_file(
            self.path_for(name),
            action=AuditAction.BACKUP_RESTORED,
            source=name,
            callers_in_flight=callers_in_flight,
            on_activated=on_activated,
        )

    def restore_external(
        self,
        upload: bytes | BinaryIO,
        *,
        callers_in_flight: int = 0,
        on_activated: Callable[[], None] | None = None,
    ) -> None:
        """Restore an upload, copied to disk first so its size costs no memory."""
        staging = self._staging_dir("import")
        try:
            staging.mkdir(parents=True)
            copied = staging / f"upload{BACKUP_SUFFIX}"
            self._copy_upload(upload, copied)
            self._restore_from_file(
                copied,
                action=AuditAction.BACKUP_IMPORTED,
                source="upload",
                callers_in_flight=callers_in_flight,
                on_activated=on_activated,
            )
        except OSError as exc:
            raise BackupError(f"could not stage the upload: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _copy_upload(upload: bytes | BinaryIO, destination: Path) -> None:
        source = io.BytesIO(upload) if isinstance(upload, bytes) else upload
        copied = 0
        with destination.open("wb") as handle:
            sink = _SyncedSink(handle)
            while block := source.read(UPLOAD_COPY_BYTES):
                copied += len(block)
                if copied > MAX_UPLOAD_BYTES:
                    raise BackupError("uploaded backup exceeds the size limit")
                sink.write(block)
            sink.sync()

    def verify(self, name: str) -> None:
        """Answer "would this archive restore?" without restoring it.

        Everything `_restore_from_file` does up to the point of no return: decrypt, which
        authenticates; extract under the traversal guard and budgets; check the manifest, its
        inventory and every checksum; and load each store the way the app would. It does
        NOT do is take the pre-restore safety archive or swap anything into place, so
        `/data` is the same afterwards whether this passes or fails — the staging directory
        it writes is its own, and it goes in `finally`.

        Until now the only way to learn an archive was unreadable was to restore it and
        find out, which is a poor moment to discover a wrong key.
        """
        path = self.path_for(name)
        staging = self._staging_dir("verify")
        try:
            self._extract_validated(path, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._audit.record(AuditAction.BACKUP_VERIFIED, source=Path(name).name)

    @contextmanager
    def _plaintext(self, path: Path, staging: Path) -> Iterator[BinaryIO]:
        """The archive's tar as a stream, in whichever format it was written.

        A version 1 archive authenticates only at its end, so it is decrypted to a file beside
        the staged members and nothing parses it until that has succeeded.
        """
        version = crypto.archive_version(path)
        if version > crypto.FILE_VERSION_CHUNKED:
            raise BackupSchemaError(f"archive uses file format {version}")
        if version == crypto.FILE_VERSION_CHUNKED:
            with path.open("rb") as source:
                yield crypto.ChunkedReader(source, self._key)
            return
        scratch = staging / LEGACY_PLAINTEXT_NAME
        try:
            with scratch.open("wb") as sink:
                crypto.decrypt_single_block_file(path, _SyncedSink(sink), self._key)
            with scratch.open("rb") as plaintext:
                yield plaintext
        finally:
            scratch.unlink(missing_ok=True)

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

    def _restore_from_file(
        self,
        path: Path,
        *,
        action: str,
        source: str,
        callers_in_flight: int,
        on_activated: Callable[[], None] | None,
    ) -> None:
        """Validate with the app still serving, then swap with nothing else running.

        `callers_in_flight` is how much of the gate's count is the caller: a request is
        counted in by the middleware. `on_activated` runs after the commit and before the
        door reopens, so no request can reach the restored data through state from before it.
        """
        staging = self._staging_dir("restore")
        try:
            self._extract_validated(path, staging)
            with self._maintenance.exclusive(callers_in_flight=callers_in_flight):
                self._activate(staging)
                self._audit.record(action, source=Path(source).name)
                if on_activated is not None:
                    on_activated()
        except MaintenanceBusyError as exc:
            raise BackupBusyError(f"restore did not start: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _activate(self, staging: Path) -> None:
        try:
            # No `keep`: a safety net taken mid-restore must never delete other archives. The
            # next create that carries a retention prunes it away.
            self.create(reason="pre-restore")
        except OSError as exc:
            raise BackupError(f"restore did not start: no safety backup: {exc}") from exc
        # The same lock `create` holds to read a consistent tree, and every filestore write
        # and audit append takes. `create` has already released it, and nothing in the swap
        # re-enters the filestore lock, so this plain (non-reentrant) lock cannot deadlock.
        with filestore.write_lock():
            try:
                self._swap_in(staging)
            except OSError as exc:
                # Rolled back by now, or `_swap_in` would have raised BackupRollbackError.
                raise BackupError(
                    f"restore failed while swapping data into place: {exc}"
                ) from exc

    def _extract_validated(self, path: Path, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        try:
            with self._plaintext(path, staging) as stream:
                with tarfile.open(fileobj=stream, mode="r|gz") as tar:
                    members = self._extract_admitted(tar, staging)
                # A tar ends before its file does, and the end is what proves nothing was cut.
                while stream.read(UPLOAD_COPY_BYTES):
                    pass
        except crypto.DecryptionError as exc:
            raise BackupKeyError(
                "backup is corrupt, tampered, or from a different key"
            ) from exc
        except _MALFORMED_ARCHIVE as exc:
            raise BackupCorruptError(f"archive cannot be read: {exc}") from exc
        except OSError as exc:
            # The route catches BackupError only, and this is not the archive's fault.
            raise BackupError(f"could not stage the archive: {exc}") from exc
        listed = self._read_manifest(staging)
        self._verify_inventory(listed, members)
        self._verify_checksums(listed, staging)
        self._verify_stores(staging)

    def _extract_admitted(
        self, tar: tarfile.TarFile, staging: Path
    ) -> list[tarfile.TarInfo]:
        """Stage every member, each judged from its header before a byte of it reaches disk.

        One pass, because the stream cannot go back: a member refused here leaves earlier ones
        in staging, which is discarded on the way out. Extracted by hand, which avoids relying
        on tarfile's `filter` argument, absent on older 3.11 patch releases (distroless runtime).
        """
        budget = _ExpansionBudget()
        members: list[tarfile.TarInfo] = []
        seen: set[str] = set()
        unsynced = 0
        for member in tar:
            self._guard_member(member)
            if member.name in seen:
                raise BackupCorruptError(f"archive lists a member twice: {member.name!r}")
            seen.add(member.name)
            budget.admit(member.name, member.size)
            members.append(member)
            self._extract_member(tar, member, staging)
            unsynced += member.size
            if unsynced >= STAGING_SYNC_BYTES:
                os.sync()
                unsynced = 0
        if MANIFEST_NAME not in seen:
            raise BackupCorruptError("archive is missing its manifest — not a BoxMedia backup")
        return members

    @staticmethod
    def _extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, staging: Path) -> None:
        destination = staging / member.name
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        extracted = tar.extractfile(member)
        with destination.open("wb") as output:
            if extracted is not None:
                shutil.copyfileobj(extracted, output)

    @staticmethod
    def _guard_member(member: tarfile.TarInfo) -> None:
        name = member.name
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts:
            raise BackupCorruptError(f"archive contains an unsafe path: {name!r}")
        if not (member.isfile() or member.isdir()):
            raise BackupCorruptError(f"archive contains a non-regular entry: {name!r}")
        # One spelling per path, so `config/./user.yml` cannot dodge the duplicate check.
        if str(PurePosixPath(name)) != name:
            raise BackupCorruptError(f"archive contains an unusual path: {name!r}")
        if name == MANIFEST_NAME and member.isfile():
            return
        # A bare file called `config` would be renamed over the live directory by the swap.
        names_a_snapshot_dir = parts[0] in _SNAPSHOT_SUBDIRS
        if not names_a_snapshot_dir or (member.isfile() and len(parts) < 2):
            raise BackupCorruptError(f"archive contains an unsupported path: {name!r}")

    def _read_manifest(self, staging: Path) -> dict[str, str]:
        """The manifest's file list, once the manifest has proved to be one."""
        path = staging / MANIFEST_NAME
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise BackupCorruptError("archive manifest is over the size limit")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both are
            raise BackupCorruptError("archive manifest is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise BackupCorruptError("archive manifest is not an object")
        version = manifest.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BackupCorruptError("archive manifest has no valid schema version")
        if version > MANIFEST_SCHEMA_VERSION:
            raise BackupSchemaError(f"archive manifest has schema version {version}")
        listed = manifest.get("files")
        if not isinstance(listed, dict):
            raise BackupCorruptError("archive manifest has no file list")
        for arcname, expected in listed.items():
            if not (isinstance(expected, str) and _SHA256_HEX.match(expected)):
                raise BackupCorruptError(f"archive manifest has a bad checksum for {arcname!r}")
        return listed

    @staticmethod
    def _verify_inventory(listed: dict[str, str], members: list[tarfile.TarInfo]) -> None:
        """The files present are exactly the files listed. A file nothing vouches for is not
        covered by any checksum, and a listed path that no member wrote may point anywhere."""
        present = {m.name for m in members if m.isfile() and m.name != MANIFEST_NAME}
        for arcname in sorted(set(listed) - present):
            raise BackupCorruptError(f"archive is missing a listed file: {arcname}")
        for arcname in sorted(present - set(listed)):
            raise BackupCorruptError(f"archive holds an unlisted file: {arcname}")

    @staticmethod
    def _verify_checksums(listed: dict[str, str], staging: Path) -> None:
        for arcname, expected in listed.items():
            with (staging / arcname).open("rb") as extracted:
                actual = hashlib.file_digest(extracted, "sha256").hexdigest()
            if actual != expected:
                raise BackupCorruptError(
                    f"checksum mismatch for {arcname} — archive is corrupt"
                )

    def _verify_stores(self, staging: Path) -> None:
        """Refuse an archive the app could not run on, BEFORE it replaces live data.

        The checksum pass above proves the bytes survived the round trip; it says nothing
        about whether they are usable. An archive from a future BoxMedia, or one whose account
        has lost a field, restores cleanly and then raises on every page, with the pre-restore
        safety archive the only way back — so the question is asked while backing out is still
        free, of the same code that would otherwise find out afterwards.
        """
        config = staging / CONFIG_SUBDIR
        if not (config / USER_FILENAME).exists():
            raise BackupCorruptError(f"archive has no account ({USER_FILENAME})")
        for filename, load in _STORE_LOADERS:
            candidate = config / filename
            if candidate.exists():
                self._check_store(candidate, partial(load, config, self._key, self._audit))
        # Sorted so the archive always names the same file first, whatever order the
        # filesystem hands them back.
        history = staging / HISTORY_SUBDIR
        reports = sorted(history.glob(f"{REPORT_FILENAME_PREFIX}*{REPORT_FILENAME_SUFFIX}"))
        store = ReportsStore(history)
        for report_file in reports:
            self._check_store(report_file, partial(store.get, report_file.stem))

    def _check_store(self, path: Path, load: Callable[[], object]) -> None:
        if path.stat().st_size > MAX_STORE_BYTES:
            raise BackupCorruptError(f"archive holds an oversized store: {path.name}")
        try:
            load()
        except filestore.SchemaVersionError as exc:
            raise BackupSchemaError(
                f"backup contains an unreadable store ({path.name}): {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — whatever a loader raises, the app would too
            # The type, not the text: pydantic quotes the offending values, and this message
            # goes to the audit log.
            raise BackupCorruptError(
                f"backup contains an unreadable store ({path.name}): {type(exc).__name__}"
            ) from exc

    def _swap_in(self, staging: Path) -> None:
        """Replace every snapshot subdir, or none of them.

        There is no atomic multi-directory rename, so the journal stands in for one: it is
        durable before the first rename, and until it says committed, both this and the next
        start put the originals back. A subdir the archive holds nothing for becomes empty
        rather than keeping live files, which would leave one install's history under
        another's settings.
        """
        stamp = f"{datetime.now(UTC):%Y%m%d%H%M%S}-{token_hex(3)}"
        retired = {subdir: f".{subdir}.retired-{stamp}" for subdir in _SNAPSHOT_SUBDIRS}
        for subdir in _SNAPSHOT_SUBDIRS:
            (staging / subdir).mkdir(exist_ok=True)
            (self._data_dir / subdir).mkdir(exist_ok=True)
        # Staged files reach the disk before the state that does not need them is given up.
        os.sync()
        maintenance.write_journal(
            self._data_dir, retired=retired, state=maintenance.STATE_ACTIVATING
        )
        try:
            for subdir in _SNAPSHOT_SUBDIRS:
                live = self._data_dir / subdir
                live.rename(self._data_dir / retired[subdir])
                (staging / subdir).rename(live)
            os.sync()  # the renames, before the journal can claim them
            maintenance.write_journal(
                self._data_dir, retired=retired, state=maintenance.STATE_COMMITTED
            )
        except OSError:
            self._roll_back_or_refuse(retired)
            raise
        maintenance.finish_commit(self._data_dir, retired)

    def _roll_back_or_refuse(self, retired: dict[str, str]) -> None:
        try:
            maintenance.roll_back(self._data_dir, retired)
        except OSError as exc:
            instructions = maintenance.recovery_instructions(self._data_dir, exc)
            print(instructions, flush=True)
            self._maintenance.refuse(instructions)
            raise BackupRollbackError(instructions) from exc
