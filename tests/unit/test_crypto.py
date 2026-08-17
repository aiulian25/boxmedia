"""Step 4 test: round-trip, wrong-key clean failure, no plaintext leakage."""

from __future__ import annotations

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


def test_rotate_constants_match_the_apps_store() -> None:
    # crypto (core) cannot import the apps store (services), so the file layout is
    # mirrored. Pin them equal here, where importing both layers is fine.
    from app.services import apps as apps_store

    assert crypto.APPS_CONFIG_PATH[-1] == apps_store.APPS_FILENAME
    assert crypto.APPS_LIST_KEY == apps_store.APPS_KEY
    assert crypto.APPS_SCHEMA_VERSION == apps_store.APPS_SCHEMA_VERSION
