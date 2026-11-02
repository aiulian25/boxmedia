"""Step 4 test: round-trip, wrong-key clean failure, no plaintext leakage."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import crypto

SECRET_API_KEY = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def test_field_round_trip() -> None:
    key = crypto.generate_key()
    token = crypto.encrypt_field(SECRET_API_KEY, key)
    assert crypto.decrypt_field(token, key) == SECRET_API_KEY


def test_field_ciphertext_contains_no_plaintext() -> None:
    key = crypto.generate_key()
    token = crypto.encrypt_field(SECRET_API_KEY, key)
    assert SECRET_API_KEY not in token
    assert crypto.is_encrypted_field(token)


def test_field_wrong_key_raises_clean_error() -> None:
    token = crypto.encrypt_field(SECRET_API_KEY, crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_field(token, crypto.generate_key())


def test_field_same_plaintext_differs_each_time() -> None:
    key = crypto.generate_key()
    assert crypto.encrypt_field(SECRET_API_KEY, key) != crypto.encrypt_field(SECRET_API_KEY, key)


def test_bytes_round_trip() -> None:
    key = crypto.generate_key()
    payload = b"\x00\x01backup tar bytes\xff" * 100
    blob = crypto.encrypt_bytes(payload, key)
    assert payload not in blob
    assert crypto.decrypt_bytes(blob, key) == payload


def test_bytes_tampered_archive_rejected() -> None:
    key = crypto.generate_key()
    blob = bytearray(crypto.encrypt_bytes(b"important data", key))
    blob[-1] ^= 0x01  # flip one bit in the tag
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(blob), key)


def test_bytes_wrong_magic_rejected() -> None:
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(b"not a backup at all", crypto.generate_key())


def test_load_key_round_trip(tmp_path: Path) -> None:
    key_file = tmp_path / "boxmedia.key"
    assert crypto._main(["genkey", str(key_file)]) == 0
    key = crypto.load_key(key_file)
    assert len(key) == crypto.KEY_LENGTH_BYTES
    # Key survives disk round-trip and decrypts what it encrypted.
    token = crypto.encrypt_field(SECRET_API_KEY, key)
    assert crypto.decrypt_field(token, key) == SECRET_API_KEY


def test_genkey_refuses_overwrite(tmp_path: Path) -> None:
    key_file = tmp_path / "boxmedia.key"
    assert crypto._main(["genkey", str(key_file)]) == 0
    assert crypto._main(["genkey", str(key_file)]) == 1


def test_load_missing_key_raises(tmp_path: Path) -> None:
    with pytest.raises(crypto.EncryptionKeyError):
        crypto.load_key(tmp_path / "nope.key")


# --- key rotation (F7) ---

SECOND_API_KEY = "ffeeddccbbaa99887766554433221100"


def _seed_apps(tmp_path: Path, key: bytes, *api_keys: str) -> Path:
    """A data dir holding an apps.yml with the given keys encrypted under `key`."""
    from app.core import filestore

    apps_path = tmp_path.joinpath(*crypto.APPS_CONFIG_PATH)
    filestore.write_yaml(
        apps_path,
        {
            crypto.APPS_LIST_KEY: [
                {
                    "id": f"app-{index}",
                    "name": f"Radarr {index}",
                    "url": "http://radarr.local:7878",
                    crypto.APPS_KEY_FIELD: crypto.encrypt_field(api_key, key),
                }
                for index, api_key in enumerate(api_keys)
            ]
        },
        schema_version=crypto.APPS_SCHEMA_VERSION,
    )
    return apps_path


def _stored_tokens(apps_path: Path) -> list[str]:
    from app.core import filestore

    document = filestore.read_yaml(apps_path, expected_version=crypto.APPS_SCHEMA_VERSION)
    return [item[crypto.APPS_KEY_FIELD] for item in document[crypto.APPS_LIST_KEY]]


PLEX_TOKEN = "sk-plex-0123456789abcdef"  # noqa: S105 — a fixture value, not a real token
TMDB_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TRAKT_ID = "Zx-9QwErTyUiOpAsDfGhJkLzXcVbNm1234567890abc"


def _seed_media_server(
    data_dir: Path, key: bytes, token: str, *, legacy: bool = False
) -> Path:
    """A media-server connection whose token is encrypted under `key`.

    `legacy` writes the pre-kinds `plex.yml` a 1.1.0 install still reads.
    """
    from app.core import filestore

    relative = (
        crypto.LEGACY_PLEX_CONFIG_PATH if legacy else crypto.MEDIA_SERVER_CONFIG_PATH
    )
    path = data_dir.joinpath(*relative)
    filestore.write_yaml(
        path,
        {
            crypto.MEDIA_SERVER_KEY: {
                "url": "http://plex.local:32400",
                crypto.MEDIA_SERVER_TOKEN_FIELD: crypto.encrypt_field(token, key),
                "kind": "plex",
            }
        },
        schema_version=crypto.MEDIA_SERVER_SCHEMA_VERSION,
    )
    return path


def _seed_discovery(data_dir: Path, key: bytes, **credentials: str) -> Path:
    """A discovery file holding whichever credential fields the caller names."""
    from app.core import filestore

    path = data_dir.joinpath(*crypto.DISCOVERY_CONFIG_PATH)
    filestore.write_yaml(
        path,
        {field: crypto.encrypt_field(value, key) for field, value in credentials.items()},
        schema_version=crypto.DISCOVERY_SCHEMA_VERSION,
    )
    return path


def _stored_media_server_token(path: Path) -> str:
    from app.core import filestore

    document = filestore.read_yaml(
        path, expected_version=crypto.MEDIA_SERVER_SCHEMA_VERSION
    )
    return document[crypto.MEDIA_SERVER_KEY][crypto.MEDIA_SERVER_TOKEN_FIELD]


def _stored_discovery(path: Path) -> dict:
    from app.core import filestore

    return filestore.read_yaml(path, expected_version=crypto.DISCOVERY_SCHEMA_VERSION)


def _two_keys(tmp_path: Path) -> tuple[Path, Path]:
    old_file, new_file = tmp_path / "old.key", tmp_path / "new.key"
    for path in (old_file, new_file):
        assert crypto._main(["genkey", str(path)]) == 0
    return old_file, new_file


def test_rotate_re_encrypts_every_key(tmp_path: Path) -> None:
    old_file, new_file = tmp_path / "old.key", tmp_path / "new.key"
    assert crypto._main(["genkey", str(old_file)]) == 0
    assert crypto._main(["genkey", str(new_file)]) == 0
    old_key, new_key = crypto.load_key(old_file), crypto.load_key(new_file)
    apps_path = _seed_apps(tmp_path / "data", old_key, SECRET_API_KEY, SECOND_API_KEY)

    assert crypto._main(["rotate", str(old_file), str(new_file), str(tmp_path / "data")]) == 0

    tokens = _stored_tokens(apps_path)
    assert [crypto.decrypt_field(token, new_key) for token in tokens] == [
        SECRET_API_KEY,
        SECOND_API_KEY,
    ]
    for token in tokens:  # the old key must no longer open them
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_field(token, old_key)


def test_rotate_with_the_wrong_old_key_changes_nothing(tmp_path: Path) -> None:
    real_file, wrong_file, new_file = (
        tmp_path / "real.key", tmp_path / "wrong.key", tmp_path / "new.key"
    )
    for path in (real_file, wrong_file, new_file):
        assert crypto._main(["genkey", str(path)]) == 0
    apps_path = _seed_apps(tmp_path / "data", crypto.load_key(real_file), SECRET_API_KEY)
    before = apps_path.read_bytes()

    assert crypto._main(["rotate", str(wrong_file), str(new_file), str(tmp_path / "data")]) == 1
    assert apps_path.read_bytes() == before  # byte-identical: nothing was written


def test_rotate_reports_a_missing_connections_file(tmp_path: Path) -> None:
    old_file, new_file = tmp_path / "old.key", tmp_path / "new.key"
    for path in (old_file, new_file):
        assert crypto._main(["genkey", str(path)]) == 0
    assert crypto._main(["rotate", str(old_file), str(new_file), str(tmp_path / "data")]) == 1


def test_rotate_rejects_a_malformed_key_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_text("not-a-key")
    new_file = tmp_path / "new.key"
    assert crypto._main(["genkey", str(new_file)]) == 0
    assert crypto._main(["rotate", str(bad), str(new_file), str(tmp_path / "data")]) == 1


def test_cli_usage_is_rejected_cleanly(tmp_path: Path) -> None:
    assert crypto._main([]) == 2
    assert crypto._main(["rotate"]) == 2  # missing arguments
    assert crypto._main(["nonsense", "x"]) == 2


def test_rotate_moves_the_media_server_token_and_the_discovery_keys(tmp_path: Path) -> None:
    """Rotation reached apps.yml and nothing else, so these two stores stayed under the
    old key — and every page that read them raised instead of rendering."""
    old_file, new_file = _two_keys(tmp_path)
    old_key, new_key = crypto.load_key(old_file), crypto.load_key(new_file)
    data_dir = tmp_path / "data"
    _seed_apps(data_dir, old_key, SECRET_API_KEY)
    server_path = _seed_media_server(data_dir, old_key, PLEX_TOKEN)
    discovery_path = _seed_discovery(
        data_dir,
        old_key,
        tmdb_key_encrypted=TMDB_KEY,
        trakt_client_id_encrypted=TRAKT_ID,
    )

    assert crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)]) == 0

    token = _stored_media_server_token(server_path)
    assert crypto.decrypt_field(token, new_key) == PLEX_TOKEN
    stored = _stored_discovery(discovery_path)
    tmdb_field, trakt_field = crypto.DISCOVERY_FIELDS
    assert crypto.decrypt_field(stored[tmdb_field], new_key) == TMDB_KEY
    assert crypto.decrypt_field(stored[trakt_field], new_key) == TRAKT_ID
    # And the old key opens none of them any more.
    for moved in (token, stored[tmdb_field], stored[trakt_field]):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_field(moved, old_key)


def test_rotate_moves_the_pre_kinds_plex_file(tmp_path: Path) -> None:
    """An install that has not re-saved its connection since 1.1.0 still reads plex.yml,
    so leaving it under the old key would strand exactly the oldest installs."""
    old_file, new_file = _two_keys(tmp_path)
    old_key, new_key = crypto.load_key(old_file), crypto.load_key(new_file)
    data_dir = tmp_path / "data"
    _seed_apps(data_dir, old_key, SECRET_API_KEY)
    legacy_path = _seed_media_server(data_dir, old_key, PLEX_TOKEN, legacy=True)

    assert crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)]) == 0

    assert crypto.decrypt_field(_stored_media_server_token(legacy_path), new_key) == PLEX_TOKEN


def test_rotate_is_all_or_nothing_across_the_stores(tmp_path: Path) -> None:
    """One unreadable discovery key leaves apps.yml untouched. Half-rotated is worse than
    not started: the half that moved can no longer be rotated with the old key."""
    old_file, new_file = _two_keys(tmp_path)
    stranger = tmp_path / "stranger.key"
    assert crypto._main(["genkey", str(stranger)]) == 0
    data_dir = tmp_path / "data"
    apps_path = _seed_apps(data_dir, crypto.load_key(old_file), SECRET_API_KEY)
    discovery_path = _seed_discovery(
        data_dir, crypto.load_key(stranger), tmdb_key_encrypted=TMDB_KEY
    )
    apps_before = apps_path.read_bytes()
    discovery_before = discovery_path.read_bytes()

    assert crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)]) == 1

    assert apps_path.read_bytes() == apps_before  # byte-identical: nothing was written
    assert discovery_path.read_bytes() == discovery_before


def test_rotate_leaves_a_media_server_record_with_no_token_alone(tmp_path: Path) -> None:
    """A hand-edited or emptied record is not a credential this key failed to open."""
    from app.core import filestore

    old_file, new_file = _two_keys(tmp_path)
    data_dir = tmp_path / "data"
    _seed_apps(data_dir, crypto.load_key(old_file), SECRET_API_KEY)
    server_path = data_dir.joinpath(*crypto.MEDIA_SERVER_CONFIG_PATH)
    filestore.write_yaml(
        server_path,
        {crypto.MEDIA_SERVER_KEY: {"url": "http://plex.local:32400", "kind": "plex"}},
        schema_version=crypto.MEDIA_SERVER_SCHEMA_VERSION,
    )
    before = server_path.read_bytes()

    assert crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)]) == 0

    assert server_path.read_bytes() == before


def test_rotate_constants_match_the_stores() -> None:
    # crypto (core) cannot import the stores (services), so the file layout of all three
    # is mirrored. Pin them equal here, where importing both layers is fine.
    from app.services import apps as apps_store
    from app.services import discovery as discovery_store
    from app.services import mediaserver as media_server_store

    assert crypto.APPS_CONFIG_PATH[-1] == apps_store.APPS_FILENAME
    assert crypto.APPS_LIST_KEY == apps_store.APPS_KEY
    assert crypto.APPS_SCHEMA_VERSION == apps_store.APPS_SCHEMA_VERSION

    assert crypto.MEDIA_SERVER_CONFIG_PATH[-1] == media_server_store.MEDIA_SERVER_FILENAME
    assert crypto.LEGACY_PLEX_CONFIG_PATH[-1] == media_server_store.LEGACY_PLEX_FILENAME
    assert crypto.MEDIA_SERVER_KEY == media_server_store.SERVER_KEY
    assert crypto.MEDIA_SERVER_TOKEN_FIELD == media_server_store.TOKEN_FIELD
    assert (
        crypto.MEDIA_SERVER_SCHEMA_VERSION
        == media_server_store.MEDIA_SERVER_SCHEMA_VERSION
    )

    assert crypto.DISCOVERY_CONFIG_PATH[-1] == discovery_store.DISCOVERY_FILENAME
    assert crypto.DISCOVERY_SCHEMA_VERSION == discovery_store.DISCOVERY_SCHEMA_VERSION
    from app.services import maintenance as maintenance_module

    assert crypto.RESTORE_JOURNAL_FILENAME == maintenance_module.JOURNAL_FILENAME

    assert crypto.DISCOVERY_FIELDS == (
        discovery_store.TMDB_KEY_FIELD,
        discovery_store.TRAKT_KEY_FIELD,
    )


# --- an interrupted rotation is resumed, not stranded ---

CRASH_EXIT_CODE = 9
# Far past the last durable step of a rotation, so the sweep knows it reached the end.
MAX_CRASH_POINTS = 40
# How many stores `_seed_every_store` writes.
STORE_COUNT = 3


def _seed_every_store(tmp_path: Path) -> tuple[Path, Path, Path]:
    old_file, new_file = _two_keys(tmp_path)
    old_key = crypto.load_key(old_file)
    data_dir = tmp_path / "data"
    _seed_apps(data_dir, old_key, SECRET_API_KEY, SECOND_API_KEY)
    _seed_media_server(data_dir, old_key, PLEX_TOKEN)
    _seed_discovery(
        data_dir, old_key, tmdb_key_encrypted=TMDB_KEY, trakt_client_id_encrypted=TRAKT_ID
    )
    return old_file, new_file, data_dir


def _assert_the_app_reads_every_credential_with(key_file: Path, data_dir: Path) -> None:
    """Through the stores the app reads with, holding the key the app would be given."""
    from app.core.audit import AuditLog
    from app.services.apps import AppsStore
    from app.services.discovery import DiscoveryStore
    from app.services.mediaserver import MediaServerStore

    key = crypto.load_key(key_file)
    config = data_dir / "config"
    audit = AuditLog(data_dir / "audit.jsonl")
    apps = AppsStore(config, key=key, audit=audit)
    assert [apps.decrypt_key(app.id) for app in apps.list_apps()] == [
        SECRET_API_KEY,
        SECOND_API_KEY,
    ]
    assert MediaServerStore(config, key=key, audit=audit).decrypt_token() == PLEX_TOKEN
    discovery = DiscoveryStore(config, key=key, audit=audit)
    assert discovery.decrypt("tmdb") == TMDB_KEY
    assert discovery.decrypt("trakt") == TRAKT_ID


def _rotate(old_file: Path, new_file: Path, data_dir: Path) -> int:
    return crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)])


def _fail_write(monkeypatch: pytest.MonkeyPatch, failing_write: int) -> None:
    from app.core import filestore

    calls = [0]
    real_write = filestore.write_yaml

    def write(*args: object, **kwargs: object) -> None:
        calls[0] += 1
        if calls[0] == failing_write:
            raise OSError(28, "No space left on device")
        real_write(*args, **kwargs)

    monkeypatch.setattr(filestore, "write_yaml", write)


@pytest.mark.parametrize("failing_write", range(1, STORE_COUNT + 1))
def test_a_write_failing_anywhere_says_what_to_do_and_a_rerun_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing_write: int,
) -> None:
    """The audit's reproduction: the second write failing left the first store on the new
    key, and a retry returned 1 because that store no longer opened with the old one."""
    old_file, new_file, data_dir = _seed_every_store(tmp_path)

    with monkeypatch.context() as patched:
        _fail_write(patched, failing_write)
        assert _rotate(old_file, new_file, data_dir) == 1
    message = capsys.readouterr().err
    assert "KEEP BOTH KEY FILES" in message
    assert "run this same command again" in message

    assert _rotate(old_file, new_file, data_dir) == 0
    _assert_the_app_reads_every_credential_with(new_file, data_dir)


@pytest.mark.parametrize("failing_write", range(1, STORE_COUNT + 1))
def test_an_interrupted_rotation_can_be_undone_by_swapping_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_write: int
) -> None:
    old_file, new_file, data_dir = _seed_every_store(tmp_path)

    with monkeypatch.context() as patched:
        _fail_write(patched, failing_write)
        assert _rotate(old_file, new_file, data_dir) == 1

    assert _rotate(new_file, old_file, data_dir) == 0
    _assert_the_app_reads_every_credential_with(old_file, data_dir)


def test_a_process_killed_at_any_durable_step_resumes_with_the_same_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real forked process, killed with nothing allowed to clean up, at each sync and
    rename in turn. It cannot show what a disk does across a power cut."""
    swept_past_the_end = False
    for crash_point in range(1, MAX_CRASH_POINTS):
        old_file, new_file, data_dir = _seed_every_store(tmp_path / str(crash_point))
        child = os.fork()
        if child == 0:
            _rotate_and_die_at(monkeypatch, crash_point, old_file, new_file, data_dir)

        _, status = os.waitpid(child, 0)
        crashed = os.waitstatus_to_exitcode(status) == CRASH_EXIT_CODE

        assert _rotate(old_file, new_file, data_dir) == 0, f"crash point {crash_point}"
        _assert_the_app_reads_every_credential_with(new_file, data_dir)
        if not crashed:
            swept_past_the_end = True
            break

    assert swept_past_the_end, "the sweep never reached the end of a rotation"


def _rotate_and_die_at(
    monkeypatch: pytest.MonkeyPatch,
    crash_point: int,
    old_file: Path,
    new_file: Path,
    data_dir: Path,
) -> None:
    seen = [0]

    def counting(real):  # noqa: ANN001, ANN202
        def step(*args: object, **kwargs: object) -> object:
            seen[0] += 1
            if seen[0] == crash_point:
                os._exit(CRASH_EXIT_CODE)
            return real(*args, **kwargs)

        return step

    monkeypatch.setattr(os, "fsync", counting(os.fsync))
    monkeypatch.setattr(os, "replace", counting(os.replace))
    try:
        _rotate(old_file, new_file, data_dir)
    finally:
        os._exit(0)


def test_a_rotation_that_already_finished_can_be_run_again(tmp_path: Path) -> None:
    """Somebody unsure whether it worked should be able to just run it, and the ciphertext
    already under the new key is left byte-identical rather than churned."""
    old_file, new_file, data_dir = _seed_every_store(tmp_path)
    assert _rotate(old_file, new_file, data_dir) == 0
    apps_path = data_dir.joinpath(*crypto.APPS_CONFIG_PATH)
    tokens = _stored_tokens(apps_path)

    assert _rotate(old_file, new_file, data_dir) == 0

    assert _stored_tokens(apps_path) == tokens
    _assert_the_app_reads_every_credential_with(new_file, data_dir)


def test_an_install_stranded_by_an_older_version_can_be_finished(tmp_path: Path) -> None:
    """Nothing about the fix depends on a journal, so a mixed state written before it existed
    resumes the same way."""
    old_file, new_file = _two_keys(tmp_path)
    old_key, new_key = crypto.load_key(old_file), crypto.load_key(new_file)
    data_dir = tmp_path / "data"
    _seed_apps(data_dir, new_key, SECRET_API_KEY, SECOND_API_KEY)
    _seed_media_server(data_dir, old_key, PLEX_TOKEN)
    _seed_discovery(
        data_dir, old_key, tmdb_key_encrypted=TMDB_KEY, trakt_client_id_encrypted=TRAKT_ID
    )

    assert _rotate(old_file, new_file, data_dir) == 0

    _assert_the_app_reads_every_credential_with(new_file, data_dir)


def test_a_credential_neither_key_opens_still_stops_everything(tmp_path: Path) -> None:
    """Accepting the new key must not widen into accepting anything."""
    old_file, new_file, data_dir = _seed_every_store(tmp_path)
    stranger = tmp_path / "stranger.key"
    assert crypto._main(["genkey", str(stranger)]) == 0
    apps_before = data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes()

    assert _rotate(stranger, new_file, data_dir) == 1

    assert data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes() == apps_before


def test_the_new_key_is_durable_before_anything_depends_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials under a key a power cut then lost would be gone for good."""
    from app.core import filestore

    old_file, new_file, data_dir = _seed_every_store(tmp_path)
    events: list[str] = []
    real_sync, real_write = crypto._sync_file, filestore.write_yaml

    def sync(path: Path) -> None:
        events.append(f"sync:{path.name}")
        real_sync(path)

    def write(*args: object, **kwargs: object) -> None:
        events.append("write")
        real_write(*args, **kwargs)

    monkeypatch.setattr(crypto, "_sync_file", sync)
    monkeypatch.setattr(filestore, "write_yaml", write)

    assert _rotate(old_file, new_file, data_dir) == 0

    assert events[0] == f"sync:{new_file.name}"
    assert events.count("write") == STORE_COUNT


def test_a_key_that_cannot_be_made_durable_stops_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_file, new_file, data_dir = _seed_every_store(tmp_path)
    apps_before = data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes()

    def fail(_: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(crypto, "_sync_file", fail)

    assert _rotate(old_file, new_file, data_dir) == 1
    assert data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes() == apps_before


def test_rotation_waits_for_an_interrupted_restore_to_be_recovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restore's rollback would put old-key config/ back after this moved the rest."""
    old_file, new_file, data_dir = _seed_every_store(tmp_path)
    (data_dir / crypto.RESTORE_JOURNAL_FILENAME).write_text("{}", encoding="utf-8")
    apps_before = data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes()

    assert _rotate(old_file, new_file, data_dir) == 1

    assert "interrupted restore" in capsys.readouterr().err
    assert data_dir.joinpath(*crypto.APPS_CONFIG_PATH).read_bytes() == apps_before


def test_genkey_explains_a_directory_it_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.core import filestore

    def refused(*_: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(filestore, "atomic_write_bytes", refused)

    assert crypto._main(["genkey", str(tmp_path / "new.key")]) == 1

    message = capsys.readouterr().err
    assert "65532" in message
    assert "init" in message


def test_genkey_still_writes_a_private_usable_key(tmp_path: Path) -> None:
    key_file = tmp_path / "nested" / "new.key"

    assert crypto._main(["genkey", str(key_file)]) == 0

    assert crypto.load_key(key_file)
    assert key_file.stat().st_mode & 0o777 == 0o600
