"""Steps 18–20 test: encrypted complete backup, indistinguishable restore, safe import."""

from __future__ import annotations

import io
import tarfile
import threading
import time
from pathlib import Path

import pytest

from app.core import crypto, filestore
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services.backup import (
    BACKUP_TIMESTAMP_FORMAT,
    MANIFEST_NAME,
    MAX_UPLOAD_BYTES,
    BackupCorruptError,
    BackupError,
    BackupKeyError,
    BackupSchemaError,
    BackupService,
    created_at_from_name,
)
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.users import UserStore

# Long enough that a racing writer is unambiguously inside the swap, short enough
# that one test does not slow the suite.
SWAP_WINDOW_SECONDS = 0.3
WAIT_TIMEOUT_SECONDS = 5.0

ORIGINAL_PASSWORD = "originalpass9word"
REPORT_ID = "report-20260812-110800-orig"


def _make_settings(tmp_path: Path, name: str) -> tuple[Settings, bytes]:
    key_file = tmp_path / f"{name}.key"
    crypto._main(["genkey", str(key_file)])
    settings = Settings(
        _env_file=None, session_secret="s" * 40, encryption_key_file=key_file,
        data_dir=tmp_path / name,
    )
    settings.ensure_data_dirs()
    return settings, crypto.load_key(key_file)


def _service(settings: Settings, key: bytes) -> BackupService:
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    return BackupService(settings.data_dir, settings.backups_dir, key=key, audit=audit)


def _seed_report(settings: Settings) -> None:
    ReportsStore(settings.history_dir).save(
        Report(
            id=REPORT_ID, run_at="2026-08-12T11:08:00+00:00", trigger=RunTrigger.MANUAL,
            status=RunStatus.OK, totals=ReportTotals(movies=1, matched=1),
            movies=[
                MovieResult(
                    rank=1, title="Neon Rain", normalized_title="neon rain",
                    gross_amount=45_000_000, gross_display="$45.0M", weeks_in_release=1,
                    status=MovieStatus.WANTED, action=MovieAction.ADDED, tmdb_id=555,
                )
            ],
        )
    )


# --- Step 18: create ---


def test_backup_is_encrypted_and_complete(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    UserStore(settings.config_dir, audit=audit).bootstrap_if_missing()
    (settings.cache_dir / "posters" / "x.jpg").write_bytes(b"poster-bytes")
    backups = _service(settings, key)

    name = backups.create()
    blob = (settings.backups_dir / name).read_bytes()

    # Not a readable tar — it is encrypted.
    with pytest.raises(tarfile.TarError):
        tarfile.open(fileobj=io.BytesIO(blob))

    # Decrypts to a tar holding the manifest and the whole data tree, minus backups/key.
    names = tarfile.open(fileobj=io.BytesIO(crypto.decrypt_bytes(blob, key))).getnames()
    assert MANIFEST_NAME in names
    assert any(n.startswith("config/") for n in names)
    assert "cache/posters/x.jpg" in names
    assert not any(n.startswith("backups") for n in names)
    assert not any(".key" in n for n in names)


def test_two_backups_in_quick_succession_do_not_collide(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    UserStore(settings.config_dir, audit=audit).bootstrap_if_missing()
    backups = _service(settings, key)
    first = backups.create()
    second = backups.create()  # same second — must not overwrite the first
    assert first != second
    assert len(backups.list_backups()) == 2


def test_create_prunes_to_keep(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    UserStore(settings.config_dir, audit=audit).bootstrap_if_missing()
    backups = _service(settings, key)
    for _ in range(3):
        backups.create(keep=2)
    assert len(backups.list_backups()) == 2  # oldest pruned, newest two kept


def test_path_for_rejects_unsafe_names(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    backups = _service(settings, key)
    with pytest.raises(BackupError):
        backups.path_for("../../etc/passwd")  # path traversal
    with pytest.raises(BackupError):
        backups.path_for("x.backup")  # right suffix, wrong prefix


# --- Step 19: internal restore, indistinguishable + safe on failure ---


def test_restore_is_indistinguishable(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    users = UserStore(settings.config_dir, audit=audit)
    users.bootstrap_if_missing()
    users.set_password(ORIGINAL_PASSWORD)
    _seed_report(settings)
    backups = _service(settings, key)

    name = backups.create()

    # Mutate: change password and delete the report.
    users.set_password("somethingelse9")
    ReportsStore(settings.history_dir).delete(REPORT_ID)
    assert ReportsStore(settings.history_dir).list_reports() == []

    backups.restore_internal(name)

    # Indistinguishable: the original password works again and the report is back.
    assert users.verify_password(ORIGINAL_PASSWORD) is True
    assert any(r.id == REPORT_ID for r in ReportsStore(settings.history_dir).list_reports())


def test_tampered_backup_leaves_live_data_untouched(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    users = UserStore(settings.config_dir, audit=audit)
    users.bootstrap_if_missing()
    users.set_password(ORIGINAL_PASSWORD)
    backups = _service(settings, key)

    name = backups.create()
    corrupted = bytearray((settings.backups_dir / name).read_bytes())
    corrupted[-1] ^= 0x01
    (settings.backups_dir / name).write_bytes(bytes(corrupted))

    with pytest.raises(BackupError):
        backups.restore_internal(name)
    # Live data unchanged.
    assert users.verify_password(ORIGINAL_PASSWORD) is True


# --- Step 20: external upload restore ---


def test_external_restore_from_another_instance(tmp_path: Path) -> None:
    key_file = tmp_path / "shared.key"
    crypto._main(["genkey", str(key_file)])
    key = crypto.load_key(key_file)

    source, _ = _make_settings(tmp_path, "source")
    source_audit = AuditLog(source.logs_dir / "audit.jsonl")
    UserStore(source.config_dir, audit=source_audit).bootstrap_if_missing()
    UserStore(source.config_dir, audit=source_audit).set_password(ORIGINAL_PASSWORD)
    _seed_report(source)
    source_backups = BackupService(source.data_dir, source.backups_dir, key=key, audit=source_audit)
    name = source_backups.create()
    blob = (source.backups_dir / name).read_bytes()

    # A fresh instance sharing the key imports the archive.
    target, _ = _make_settings(tmp_path, "target")
    target_backups = _service(target, key)
    target_backups.restore_external(blob)

    target_users = UserStore(target.config_dir, audit=AuditLog(target.logs_dir / "a.jsonl"))
    assert target_users.verify_password(ORIGINAL_PASSWORD) is True
    assert any(r.id == REPORT_ID for r in ReportsStore(target.history_dir).list_reports())


def test_external_restore_rejects_tampered(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    backups = _service(settings, key)
    blob = bytearray(crypto.encrypt_bytes(b"not even a tar", key))
    blob[-1] ^= 0x01
    with pytest.raises(BackupError):
        backups.restore_external(bytes(blob))


def test_external_restore_rejects_oversized(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    backups = _service(settings, key)
    with pytest.raises(BackupError):
        backups.restore_external(b"x" * (MAX_UPLOAD_BYTES + 1))


def test_external_restore_rejects_path_traversal(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    backups = _service(settings, key)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member_name, payload in (("../evil.yml", b"pwn"), (MANIFEST_NAME, b'{"files":{}}')):
            info = tarfile.TarInfo(name=member_name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    malicious = crypto.encrypt_bytes(buffer.getvalue(), key)
    with pytest.raises(BackupError):
        backups.restore_external(malicious)
    assert not (tmp_path / "evil.yml").exists()


def test_backup_info_carries_its_creation_time(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "data")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    UserStore(settings.config_dir, audit=audit).bootstrap_if_missing()
    backups = _service(settings, key)

    name = backups.create()
    info = backups.list_backups()[0]
    assert info.name == name
    assert info.created_at is not None
    # Read from the name, so it matches the archive's own stamp exactly.
    assert info.created_at.strftime(BACKUP_TIMESTAMP_FORMAT) in name
    assert info.created_at.tzinfo is not None  # timezone-aware (UTC)


def test_created_at_from_name_handles_odd_names() -> None:
    assert created_at_from_name("not-a-backup.txt") is None
    assert created_at_from_name("boxmedia-nonsense.backup") is None
    parsed = created_at_from_name("boxmedia-20260814-102956-eada.backup")
    assert (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 8, 14, 10)


# --- the swap runs under the filestore write lock (review step 4) ---


def test_the_swap_holds_the_filestore_write_lock(tmp_path: Path) -> None:
    """`create` quiesces writers to READ a consistent tree; the swap that replaces that
    tree has to do the same, or a concurrent write lands in a directory about to be
    renamed away and deleted."""
    settings, key = _make_settings(tmp_path, "locked")
    service = _service(settings, key)
    _seed_report(settings)
    name = service.create()

    held: list[bool] = []
    original_swap = service._swap_in

    def recording_swap(staging: Path) -> None:
        held.append(filestore.write_lock().locked())
        original_swap(staging)

    service._swap_in = recording_swap
    service.restore_internal(name)

    assert held == [True]
    assert not filestore.write_lock().locked()  # and released afterwards


def test_a_failed_swap_is_a_backup_error_not_an_oserror(tmp_path: Path) -> None:
    """The route catches BackupError and shows "nothing was changed"; a bare OSError
    escapes it and 500s on a half-swapped tree."""
    settings, key = _make_settings(tmp_path, "swapfail")
    service = _service(settings, key)
    _seed_report(settings)
    name = service.create()

    def failing_swap(staging: Path) -> None:
        raise OSError(39, "Directory not empty")

    service._swap_in = failing_swap
    with pytest.raises(BackupError) as caught:
        service.restore_internal(name)
    assert "swapping data into place" in str(caught.value)


def test_the_lock_is_released_after_a_failed_swap(tmp_path: Path) -> None:
    # A lock left held by a failed restore would freeze every later write in the process.
    settings, key = _make_settings(tmp_path, "swapfail2")
    service = _service(settings, key)
    _seed_report(settings)
    name = service.create()
    service._swap_in = lambda staging: (_ for _ in ()).throw(OSError("boom"))

    with pytest.raises(BackupError):
        service.restore_internal(name)

    assert not filestore.write_lock().locked()


def test_a_swap_that_fails_partway_leaves_every_original_recoverable(tmp_path: Path) -> None:
    """There is no atomic multi-directory rename, so a partial swap is possible.

    Deleting each retired directory inside the loop destroyed the originals of the
    subdirs already swapped, while the restore as a whole had failed.
    """
    settings, key = _make_settings(tmp_path, "partial")
    service = _service(settings, key)
    # config/ is swapped FIRST and history/ second, so failing on history means config
    # has already been renamed away by the time the restore aborts.
    marker = settings.config_dir / "marker-config.txt"
    marker.write_text("the state that must survive a failed restore", encoding="utf-8")
    _seed_report(settings)
    name = service.create()

    real_rename = Path.rename

    def rename_failing_on_history(self: Path, target: object) -> object:
        if self.name == "history":
            raise OSError(39, "Directory not empty")
        return real_rename(self, target)

    Path.rename = rename_failing_on_history
    try:
        with pytest.raises(BackupError):
            service.restore_internal(name)
    finally:
        Path.rename = real_rename

    retired = list(settings.data_dir.glob(".config.retired-*"))
    assert retired, "config/'s original was deleted despite the restore failing"
    survivor = retired[0] / marker.name
    assert survivor.exists()
    assert "must survive" in survivor.read_text(encoding="utf-8")


def test_a_successful_restore_leaves_no_retired_directories(tmp_path: Path) -> None:
    # The deferred cleanup must still happen on the happy path — no slow disk leak.
    settings, key = _make_settings(tmp_path, "cleanup")
    service = _service(settings, key)
    _seed_report(settings)
    name = service.create()

    service.restore_internal(name)

    assert list(settings.data_dir.glob(".*.retired-*")) == []
    assert list(settings.data_dir.glob(".restore-staging-*")) == []


def test_a_racing_write_cannot_land_in_a_doomed_directory(tmp_path: Path) -> None:
    """The data-loss scenario itself: a scheduled run writing a report mid-restore.

    Without the lock the write COMPLETES inside the swap window — into the directory
    that is renamed to `.retired-*` and deleted an instant later, so the write is lost
    with no error anywhere. With it, the writer blocks and lands in the restored tree.
    """
    settings, key = _make_settings(tmp_path, "race")
    service = _service(settings, key)
    _seed_report(settings)
    name = service.create()

    events: list[str] = []
    swap_started = threading.Event()
    real_swap = service._swap_in

    def slow_swap(staging: Path) -> None:
        events.append("swap-start")
        swap_started.set()
        time.sleep(SWAP_WINDOW_SECONDS)  # the window a racing writer would exploit
        real_swap(staging)
        events.append("swap-end")

    service._swap_in = slow_swap

    def racing_writer() -> None:
        swap_started.wait(timeout=WAIT_TIMEOUT_SECONDS)
        events.append("write-start")
        filestore.write_json(
            settings.history_dir / "racer.json", {"x": 1}, schema_version=1
        )
        events.append("write-end")

    writer = threading.Thread(target=racing_writer)
    writer.start()
    service.restore_internal(name)
    writer.join(timeout=WAIT_TIMEOUT_SECONDS)

    assert events.index("write-start") < events.index("swap-end")  # it really did race
    assert events.index("write-end") > events.index("swap-end")    # but had to wait


# --- restore refuses what this build cannot read (review step 5) ---


def _archive_with(settings: Settings, key: bytes, extra: dict[str, str]) -> bytes:
    """A real archive of `settings`, plus/overwriting the given files, re-encrypted.

    Built by round-tripping through the service so the manifest checksums match — the
    point is to get past the checksum pass and be caught by the schema pass.
    """
    for relative, body in extra.items():
        target = settings.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    service = _service(settings, key)
    name = service.create()
    return (settings.backups_dir / name).read_bytes()


def test_a_report_from_a_newer_build_is_refused_before_the_swap(tmp_path: Path) -> None:
    """The reproduction from the review: one report stamped schema_version 2 used to
    restore cleanly and then crash /dashboard and /reports with SchemaVersionError —
    after live data had already been replaced."""
    source, key = _make_settings(tmp_path, "future")
    blob = _archive_with(
        source, key,
        {"history/report-20990101-000000-beef.json": '{"schema_version": 2, "id": "x"}'},
    )

    target, _ = _make_settings(tmp_path, "target")
    marker = target.config_dir / "live.txt"
    marker.write_text("untouched", encoding="utf-8")

    with pytest.raises(BackupError) as caught:
        _service(target, key).restore_external(blob)

    assert "unreadable report" in str(caught.value)
    assert marker.read_text(encoding="utf-8") == "untouched"  # nothing was swapped
    assert list(target.data_dir.glob(".*.retired-*")) == []


def test_a_config_store_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    source, key = _make_settings(tmp_path, "futurecfg")
    blob = _archive_with(source, key, {"config/apps.yml": "schema_version: 99\napps: []\n"})

    target, _ = _make_settings(tmp_path, "targetcfg")
    with pytest.raises(BackupError) as caught:
        _service(target, key).restore_external(blob)
    assert "unreadable store" in str(caught.value)
    assert "apps.yml" in str(caught.value)


def test_malformed_yaml_is_refused_rather_than_escaping(tmp_path: Path) -> None:
    """yaml raises ParserError, which is NOT a ValueError.

    Caught only by listing yaml.YAMLError: otherwise it sails past this check AND the
    route's `except BackupError` and 500s on an already-replaced tree.
    """
    source, key = _make_settings(tmp_path, "badyaml")
    blob = _archive_with(source, key, {"config/user.yml": "key: [unclosed\n  bad: : :"})

    target, _ = _make_settings(tmp_path, "targetyaml")
    with pytest.raises(BackupError) as caught:
        _service(target, key).restore_external(blob)
    assert "unreadable store" in str(caught.value)


def test_a_healthy_archive_still_restores(tmp_path: Path) -> None:
    # The check must not become a wall: a normal archive passes it untouched.
    settings, key = _make_settings(tmp_path, "healthy")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    users = UserStore(settings.config_dir, audit=audit)
    users.bootstrap_if_missing()
    users.set_password(ORIGINAL_PASSWORD)
    _seed_report(settings)
    service = _service(settings, key)
    name = service.create()

    service.restore_internal(name)

    assert users.verify_password(ORIGINAL_PASSWORD) is True
    assert any(r.id == REPORT_ID for r in ReportsStore(settings.history_dir).list_reports())


def test_the_mirrored_filters_constants_match_the_real_ones() -> None:
    """app.services.filters imports backup, so its schema version is mirrored here
    rather than imported. Pinned equal so the mirror cannot silently drift."""
    from app.services import backup as backup_module
    from app.services import filters as filters_module

    assert backup_module.MIRRORED_FILTERS_FILENAME == filters_module.FILTERS_FILENAME
    assert (
        backup_module.MIRRORED_FILTERS_SCHEMA_VERSION
        == filters_module.FILTERS_SCHEMA_VERSION
    )


def test_every_schema_stamped_store_is_covered(tmp_path: Path) -> None:
    """A new store added to config/ without an entry here would restore unvalidated.

    Compares the table against what a real backup of a fully populated install holds.
    """
    from app.services.backup import _STORE_VERSIONS

    settings, key = _make_settings(tmp_path, "coverage")
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    UserStore(settings.config_dir, audit=audit).bootstrap_if_missing()
    for name, body in (
        ("apps.yml", "schema_version: 1\napps: []\n"),
        ("filters.yml", "schema_version: 1\n"),
        ("ignored.yml", "schema_version: 1\nignored: []\n"),
        ("radarr_options.yml", "schema_version: 2\nby_app: {}\n"),
    ):
        (settings.config_dir / name).write_text(body, encoding="utf-8")

    checked = {relative for relative, _, _ in _STORE_VERSIONS}
    on_disk = {
        f"config/{path.name}" for path in settings.config_dir.iterdir() if path.is_file()
    }
    assert on_disk <= checked, f"unvalidated store(s): {on_disk - checked}"


# --- the specific reasons are still one family (F17) ---


def test_every_specific_reason_is_still_a_backup_error() -> None:
    """The scheduler catches `(BackupError, OSError)` and swallows it so a missed backup
    cannot stop the clock. Splitting the reasons out must not let one escape that — which
    subclassing guarantees, and this asserts rather than assumes.
    """
    for specific in (BackupKeyError, BackupSchemaError, BackupCorruptError):
        assert issubclass(specific, BackupError)
        assert isinstance(specific("why"), BackupError)


def test_verify_reads_back_what_create_wrote(tmp_path: Path) -> None:
    """The service's own contract, under the routes: an archive this build just made is
    one it can restore, and the same archive under another key is not."""
    settings, key = _make_settings(tmp_path, "data")
    _seed_report(settings)
    service = _service(settings, key)
    name = service.create()

    service.verify(name)  # raises nothing

    path = service.path_for(name)
    good = path.read_bytes()
    path.write_bytes(
        crypto.encrypt_bytes(crypto.decrypt_bytes(good, key), crypto.generate_key())
    )
    with pytest.raises(BackupKeyError):
        service.verify(name)

    path.write_bytes(good)
    service.verify(name)  # readable again — nothing was consumed by the failure


def test_verify_leaves_no_staging_directory_behind(tmp_path: Path) -> None:
    """Its own prefix, so a verify and a restore begun in the same second cannot share a
    directory and delete each other's in `finally`."""
    settings, key = _make_settings(tmp_path, "data")
    service = _service(settings, key)
    name = service.create()

    service.verify(name)

    assert not list(settings.data_dir.glob(".verify-staging-*"))
    assert not list(settings.data_dir.glob(".restore-staging-*"))
