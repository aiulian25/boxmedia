"""What Verify and Restore refuse, and what they must keep accepting.

Every archive here is sealed with the install's OWN key, because that is the case the audit
reproduced: GCM proves the bytes are ours, and says nothing about whether they are usable. An
intact archive holding a `user.yml` with nothing but its schema stamp used to pass Verify and
Restore, and then nobody could log in.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core import crypto
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services import backup as backup_module
from app.services.backup import (
    MANIFEST_NAME,
    BackupCorruptError,
    BackupError,
    BackupSchemaError,
    BackupService,
)
from app.services.reports import ReportsStore
from app.services.users import UserStore
from tests.integration.test_backup import REPORT_ID, _make_settings, _seed_report

USER_PATH = "config/user.yml"
REPORT_PATH = f"history/{REPORT_ID}.json"
SHA256_OF_NOTHING = hashlib.sha256(b"").hexdigest()
Members = dict[str, bytes]


def _service(settings: Settings, key: bytes) -> BackupService:
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    return BackupService(settings.data_dir, settings.backups_dir, key=key, audit=audit)


@pytest.fixture
def install(tmp_path: Path) -> tuple[Settings, bytes, BackupService]:
    settings, key = _make_settings(tmp_path, "data")
    _seed_report(settings)
    (settings.cache_dir / "posters" / "x.jpg").write_bytes(b"poster-bytes")
    return settings, key, _service(settings, key)


def _members_of(service: BackupService, name: str, key: bytes) -> Members:
    plaintext = crypto.decrypt_bytes(service.path_for(name).read_bytes(), key)
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
        }


def _tar(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for arcname, data in entries:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _relisted(members: Members) -> Members:
    """The same files under a manifest that vouches for them, so checksums pass."""
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["files"] = {
        arcname: hashlib.sha256(data).hexdigest()
        for arcname, data in members.items()
        if arcname != MANIFEST_NAME
    }
    return {**members, MANIFEST_NAME: json.dumps(manifest).encode()}


def _stored(service: BackupService, key: bytes, plaintext: bytes) -> str:
    """`plaintext` sealed with this install's key, as an archive sitting in its backups."""
    name = service.create()
    service.path_for(name).write_bytes(crypto.encrypt_bytes(plaintext, key))
    return name


def _sealed(service: BackupService, key: bytes, members: Members) -> str:
    return _stored(service, key, _tar(list(_relisted(members).items())))


def _assert_refused_and_harmless(
    settings: Settings, service: BackupService, name: str, expected: type[BackupError]
) -> None:
    """Verify and Restore both refuse with the expected reason, and live data never moves."""
    live_user = (settings.config_dir / "user.yml").read_bytes()
    blob = service.path_for(name).read_bytes()

    with pytest.raises(expected):
        service.verify(name)
    with pytest.raises(expected):
        service.restore_external(blob)

    assert (settings.config_dir / "user.yml").read_bytes() == live_user
    assert list(settings.data_dir.glob(".*-staging-*")) == []
    assert list(settings.data_dir.glob(".*.retired-*")) == []


# --- a store the app could not run on ---


UNUSABLE_STORES = {
    "the account has only its stamp": (USER_PATH, b"schema_version: 1\n"),
    "the account hash is not a hash": (
        USER_PATH,
        b"schema_version: 1\nusername: admin\ndisplay_name: A\nemail: a@b.c\n"
        b"password_hash: not-a-hash\nmust_change_password: false\n",
    ),
    "the account has no username": (
        USER_PATH,
        b"schema_version: 1\nusername: ''\ndisplay_name: A\nemail: a@b.c\n"
        b"password_hash: $argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo\n"
        b"must_change_password: false\n",
    ),
    "a connection lost its key": ("config/apps.yml", b"schema_version: 1\napps:\n- id: app-1\n"),
    "filters hold an impossible value": (
        "config/filters.yml",
        b"schema_version: 1\nbackup_keep: 0\n",
    ),
    "a correction has no film": (
        "config/corrections.yml",
        b"schema_version: 1\nby_title:\n  neon rain:\n    title: Neon Rain\n",
    ),
    "an ignore entry has no title": ("config/ignored.yml", b"schema_version: 1\nignored:\n- {}\n"),
    "radarr options have the wrong shape": (
        "config/radarr_options.yml",
        b"schema_version: 2\nby_app:\n  app-1: 7\n",
    ),
    "sonarr options have the wrong shape": (
        "config/sonarr_options.yml",
        b"schema_version: 2\nby_app:\n  app-1: 7\n",
    ),
    "a report has no id": (REPORT_PATH, b'{"schema_version": 1, "movies": []}'),
    "a report is not JSON": (REPORT_PATH, b"{"),
    "a store is not a mapping": ("config/ignored.yml", b"- just\n- a list\n"),
    "a store is malformed": ("config/filters.yml", b"key: [unclosed\n  bad: : :"),
}


@pytest.mark.parametrize("path, body", UNUSABLE_STORES.values(), ids=UNUSABLE_STORES.keys())
def test_a_store_the_app_could_not_run_on_is_refused(
    install: tuple[Settings, bytes, BackupService], path: str, body: bytes
) -> None:
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    members[path] = body

    name = _sealed(service, key, members)

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_an_archive_with_no_account_is_refused(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    del members[USER_PATH]

    name = _sealed(service, key, members)

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_refusal_never_quotes_the_stores_contents(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    """The verify route writes this message into the audit log, and pydantic's own error
    quotes the values it rejected."""
    _, key, service = install
    members = _members_of(service, service.create(), key)
    members["config/corrections.yml"] = (
        b"schema_version: 1\nby_title:\n  x:\n    tmdb_id: sensitive-value\n    title: T\n"
    )
    name = _sealed(service, key, members)

    with pytest.raises(BackupCorruptError) as caught:
        service.verify(name)

    assert "corrections.yml" in str(caught.value)
    assert "sensitive-value" not in str(caught.value)


@pytest.mark.parametrize(
    "path, body",
    [
        (USER_PATH, b"schema_version: 99\n"),
        ("config/corrections.yml", b"schema_version: 99\nby_title: {}\n"),
        ("config/filters.yml", b"schema_version: 99\n"),
        (REPORT_PATH, b'{"schema_version": 99}'),
    ],
    ids=["account", "corrections", "filters", "report"],
)
def test_a_store_from_a_newer_build_says_to_upgrade(
    install: tuple[Settings, bytes, BackupService], path: str, body: bytes
) -> None:
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    members[path] = body

    name = _sealed(service, key, members)

    _assert_refused_and_harmless(settings, service, name, BackupSchemaError)


# --- the manifest and the file list ---


def _with_manifest(service: BackupService, key: bytes, manifest: bytes) -> str:
    members = _members_of(service, service.create(), key)
    members[MANIFEST_NAME] = manifest
    return _stored(service, key, _tar(list(members.items())))


@pytest.mark.parametrize(
    "manifest",
    [
        b"{",
        b"\xff\xfe",
        b"[]",
        b'"just a string"',
        json.dumps({"files": {}}).encode(),
        json.dumps({"schema_version": "1", "files": {}}).encode(),
        json.dumps({"schema_version": True, "files": {}}).encode(),
        json.dumps({"schema_version": 0, "files": {}}).encode(),
        json.dumps({"schema_version": 1}).encode(),
        json.dumps({"schema_version": 1, "files": []}).encode(),
        json.dumps({"schema_version": 1, "files": {USER_PATH: 7}}).encode(),
        json.dumps({"schema_version": 1, "files": {USER_PATH: "short"}}).encode(),
    ],
    ids=[
        "truncated", "not-utf8", "a-list", "a-string", "no-version", "string-version",
        "boolean-version", "zero-version", "no-files", "files-a-list", "checksum-a-number",
        "checksum-too-short",
    ],
)
def test_a_malformed_manifest_is_refused(
    install: tuple[Settings, bytes, BackupService], manifest: bytes
) -> None:
    settings, key, service = install
    name = _with_manifest(service, key, manifest)

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_manifest_from_a_newer_build_says_to_upgrade(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    settings, key, service = install
    name = _with_manifest(service, key, json.dumps({"schema_version": 99, "files": {}}).encode())

    _assert_refused_and_harmless(settings, service, name, BackupSchemaError)


def test_a_file_the_manifest_does_not_list_is_refused(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    """No checksum vouches for it, and the swap would still make it live."""
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    members["config/planted.yml"] = b"anything"

    name = _stored(service, key, _tar(list(members.items())))

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_listed_path_outside_the_archive_is_never_read(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    """A manifest entry is a path too. Checked against what the members wrote, it cannot walk
    out of staging to hash, or even stat, a file of the host's."""
    settings, key, service = install
    outside = settings.data_dir.parent / "outside.txt"
    outside.write_bytes(b"")
    members = _members_of(service, service.create(), key)
    manifest = json.loads(_relisted(members)[MANIFEST_NAME])
    manifest["files"]["../../outside.txt"] = SHA256_OF_NOTHING
    members[MANIFEST_NAME] = json.dumps(manifest).encode()

    name = _stored(service, key, _tar(list(members.items())))

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


@pytest.mark.parametrize(
    "entries",
    [
        [(USER_PATH, b"first"), (USER_PATH, b"second")],
        [(USER_PATH, b"first"), (f"config/./{Path(USER_PATH).name}", b"second")],
        [("backups/planted.backup", b"x")],
        [("stray.txt", b"x")],
        [("config", b"a file where the directory goes")],
        [("config/user.yml/below", b"x")],
    ],
    ids=[
        "duplicate", "duplicate-by-spelling", "the-backups-dir", "top-level-file",
        "a-file-named-config", "a-path-through-a-file",
    ],
)
def test_a_member_nothing_could_have_written_is_refused(
    install: tuple[Settings, bytes, BackupService], entries: list[tuple[str, bytes]]
) -> None:
    settings, key, service = install
    members = _members_of(service, service.create(), key)

    name = _stored(service, key, _tar([*members.items(), *entries]))

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda plaintext: b"this is not a gzip stream",
        lambda plaintext: plaintext[: len(plaintext) // 2],
        lambda plaintext: b"",
    ],
    ids=["not-gzip", "truncated", "empty"],
)
def test_authentic_bytes_that_are_not_an_archive_are_refused_not_a_500(
    install: tuple[Settings, bytes, BackupService], mangle: Callable[[bytes], bytes]
) -> None:
    """These decrypt, so the tag check passes them along. tarfile, gzip and zlib each raise
    their own type, and none of them is one the route catches."""
    settings, key, service = install
    source = service.create()
    plaintext = crypto.decrypt_bytes(service.path_for(source).read_bytes(), key)

    name = _stored(service, key, mangle(plaintext))

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_full_disk_is_not_called_corruption(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy differs: nothing is wrong with the archive, so another one will not help.
    Still a BackupError, or the route would answer with a 500."""
    _, _, service = install
    name = service.create()

    def disk_full(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup_module.shutil, "copyfileobj", disk_full)

    with pytest.raises(BackupError) as caught:
        service.verify(name)
    assert type(caught.value) is BackupError


# --- budgets ---


def test_a_small_archive_that_unpacks_large_is_refused_before_it_reaches_the_disk(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compressed small and expanded large, measured from the header alone, so not a byte of
    it is written. Members before it are staged, and discarded with the staging directory."""
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    large = b"\0" * (backup_module.MAX_MEMBER_BYTES + 1)
    plaintext = _tar([*members.items(), ("cache/posters/large.jpg", large)])
    assert len(plaintext) < len(large) // 100
    name = _stored(service, key, plaintext)
    staged: list[object] = []
    monkeypatch.setattr(backup_module.shutil, "copyfileobj", lambda *_: staged.append(_))

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)

    assert not any(destination.name.endswith("large.jpg") for _, destination in staged)


@pytest.mark.parametrize(
    "limit, value",
    [("MAX_ARCHIVE_MEMBERS", 2), ("MAX_EXPANDED_BYTES", 8), ("MAX_MEMBER_BYTES", 8)],
)
def test_each_budget_is_enforced_when_reading(
    install: tuple[Settings, bytes, BackupService],
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
) -> None:
    settings, _, service = install
    name = service.create()
    monkeypatch.setattr(backup_module, limit, value)

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_store_too_large_to_parse_is_refused_unparsed(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _, service = install
    name = service.create()
    monkeypatch.setattr(backup_module, "MAX_STORE_BYTES", 8)
    monkeypatch.setattr(
        backup_module.UserStore, "validate_stored", lambda _: pytest.fail("it was parsed")
    )

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_manifest_too_large_to_parse_is_refused_unparsed(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _, service = install
    name = service.create()
    monkeypatch.setattr(backup_module, "MAX_MANIFEST_BYTES", 8)

    _assert_refused_and_harmless(settings, service, name, BackupCorruptError)


def test_a_large_extraction_reaches_the_disk_as_it_goes(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsynced writes count against the container's limit, and at 256 MiB the kernel killed
    a 2 GiB extraction whose process was only 68 MiB."""
    _, _, service = install
    name = service.create()
    syncs: list[None] = []
    monkeypatch.setattr(backup_module, "STAGING_SYNC_BYTES", 1)
    monkeypatch.setattr(backup_module.os, "sync", lambda: syncs.append(None))

    service.verify(name)

    assert syncs


def test_create_will_not_write_an_archive_it_would_refuse_to_read(
    install: tuple[Settings, bytes, BackupService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found out on the night it is needed otherwise. A BackupError, so the scheduler logs it
    and carries on, and no archive is left behind for retention to prefer over a good one."""
    settings, _, service = install
    before = service.list_backups()
    monkeypatch.setattr(backup_module, "MAX_MEMBER_BYTES", 8)

    with pytest.raises(BackupError):
        service.create()

    assert service.list_backups() == before


# --- what must keep working ---


def test_a_real_archive_verifies_and_restores_with_every_store_present(tmp_path: Path) -> None:
    settings, key = _make_settings(tmp_path, "full")
    _seed_report(settings)
    for filename, body in (
        ("apps.yml", "schema_version: 1\napps:\n- {id: app-1, name: R, url: 'http://r', "
                     "api_key_encrypted: ciphertext}\n"),
        ("filters.yml", "schema_version: 1\n"),
        ("ignored.yml", "schema_version: 1\nignored:\n- {title: T, normalized_title: t}\n"),
        ("corrections.yml", "schema_version: 1\nby_title:\n  t: {tmdb_id: 1, title: T}\n"),
        ("radarr_options.yml", "schema_version: 2\nby_app: {}\n"),
        ("sonarr_options.yml", "schema_version: 2\nby_app: {}\n"),
        ("mediaserver.yml", "schema_version: 1\nserver: {url: 'http://m', token: ciphertext}\n"),
        ("discovery.yml", "schema_version: 1\n"),
    ):
        (settings.config_dir / filename).write_text(body, encoding="utf-8")
    service = _service(settings, key)

    name = service.create()
    service.verify(name)
    service.restore_internal(name)

    assert any(r.id == REPORT_ID for r in ReportsStore(settings.history_dir).list_reports())


LEGACY_SHAPES = {
    "an account from before themes": (
        USER_PATH,
        lambda body: b"\n".join(
            line for line in body.splitlines() if not line.startswith(b"theme")
        ),
    ),
    "radarr options from v1": (
        "config/radarr_options.yml",
        lambda _: b"schema_version: 1\nquality_profiles: []\n",
    ),
    "media server from before kinds": (
        "config/plex.yml",
        lambda _: b"schema_version: 1\nserver: {url: 'http://p', token: ciphertext}\n",
    ),
    "a report from before regions": (
        REPORT_PATH,
        lambda body: json.dumps(
            {k: v for k, v in json.loads(body).items() if k not in {"region", "period"}}
        ).encode(),
    ),
    "a cache this build cannot read": ("cache/calendar.json", lambda _: b'{"schema_version": 99}'),
    "a damaged cache": ("cache/sonarr-library.json", lambda _: b"{"),
    "an unstamped log": ("logs/audit.jsonl", lambda _: b"not json\n"),
}


@pytest.mark.parametrize("path, change", LEGACY_SHAPES.values(), ids=LEGACY_SHAPES.keys())
def test_older_shapes_and_harmless_caches_still_restore(
    install: tuple[Settings, bytes, BackupService],
    path: str,
    change: Callable[[bytes], bytes],
) -> None:
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    members[path] = change(members.get(path, b""))
    name = _sealed(service, key, members)

    service.verify(name)
    service.restore_internal(name)


def test_absent_optional_stores_are_not_corruption(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    """The account is the only store an install cannot run without."""
    settings, key, service = install
    members = _members_of(service, service.create(), key)
    kept = {
        arcname: data
        for arcname, data in members.items()
        if arcname in {MANIFEST_NAME, USER_PATH}
    }
    name = _sealed(service, key, kept)

    service.verify(name)
    service.restore_internal(name)

    assert UserStore(settings.config_dir, audit=service._audit).exists()


def test_directory_members_are_tolerated(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    """Our own archives have none, but a tar re-packed by hand does, and nothing is wrong."""
    _, key, service = install
    members = _relisted(_members_of(service, service.create(), key))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        directory = tarfile.TarInfo(name="config")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        for arcname, data in members.items():
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    service.verify(_stored(service, key, buffer.getvalue()))


def test_a_saved_credential_the_key_cannot_read_does_not_block_a_restore(
    tmp_path: Path,
) -> None:
    """The app runs on with one, says so, and asks for it again. Refusing here would lock a
    whole history behind it."""
    settings, key = _make_settings(tmp_path, "stale")
    foreign = crypto.encrypt_field("radarr-key", crypto.generate_key())
    (settings.config_dir / "apps.yml").write_text(
        "schema_version: 1\napps:\n"
        f"- {{id: app-1, name: R, url: 'http://r', api_key_encrypted: '{foreign}'}}\n",
        encoding="utf-8",
    )
    service = _service(settings, key)

    service.verify(service.create())


def test_a_damaged_attempt_record_is_refused_and_a_good_one_restores(
    install: tuple[Settings, bytes, BackupService],
) -> None:
    from datetime import UTC, datetime

    from app.services.ingestion_state import INGESTION_FILENAME, IngestionStateStore

    settings, key, service = install
    IngestionStateStore(settings.config_dir).record_attempt(datetime.now(UTC))
    name = service.create()

    service.verify(name)

    members = _members_of(service, name, key)
    members[f"config/{INGESTION_FILENAME}"] = b"schema_version: 1\nattempted_at: 7\n"
    bad = _sealed(service, key, members)
    _assert_refused_and_harmless(settings, service, bad, BackupCorruptError)
