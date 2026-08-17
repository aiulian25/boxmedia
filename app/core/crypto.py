"""AES-256-GCM encryption for stored Radarr API keys and backup archives.

Chosen over age/gpg because the distroless runtime has no shell or package
manager to invoke external binaries (Step 4 rationale). The key is loaded from
`BM_ENCRYPTION_KEY_FILE`, which lives outside the data directory so a backup of
`/data` can never contain the key that decrypts it.

Field format:  gcm:v1:<b64url(nonce)>:<b64url(ciphertext+tag)>
File format:   4-byte magic | 1-byte version | 12-byte nonce | ciphertext+tag
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LENGTH_BYTES = 32  # AES-256
NONCE_LENGTH_BYTES = 12  # GCM standard nonce
FIELD_PREFIX = "gcm"
FIELD_VERSION = "v1"
FILE_MAGIC = b"BMB1"  # BoxMedia Backup, format 1
FILE_VERSION = 1

# Where the encrypted Radarr keys live, relative to the data dir. Deliberately mirrored
# from the apps store rather than imported — core must never import services — and a test
# pins these equal to `app.services.apps` so they cannot drift.
APPS_CONFIG_PATH = ("config", "apps.yml")
APPS_SCHEMA_VERSION = 1
APPS_LIST_KEY = "apps"
APPS_KEY_FIELD = "api_key_encrypted"

USAGE = (
    "usage:\n"
    "  python -m app.core.crypto genkey <key-file>\n"
    "  python -m app.core.crypto rotate <old-key-file> <new-key-file> <data-dir>\n"
    "\n"
    "rotate re-encrypts the stored Radarr API keys. Stop BoxMedia first."
)


class DecryptionError(Exception):
    """Ciphertext could not be authenticated/decrypted with the provided key."""


class EncryptionKeyError(Exception):
    """The encryption key file is missing or malformed."""


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=KEY_LENGTH_BYTES * 8)


def load_key(key_file: Path) -> bytes:
    if not key_file.exists():
        raise EncryptionKeyError(
            f"encryption key file not found: {key_file} "
            f"(create it with `python -m app.core.crypto genkey {key_file}`)"
        )
    raw = key_file.read_bytes().strip()
    try:
        key = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyError(f"encryption key file {key_file} is not valid base64") from exc
    if len(key) != KEY_LENGTH_BYTES:
        raise EncryptionKeyError(
            f"encryption key must be {KEY_LENGTH_BYTES} bytes, got {len(key)} from {key_file}"
        )
    return key


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def encrypt_field(plaintext: str, key: bytes) -> str:
    """Encrypt a short string (e.g. a Radarr API key) into the field format."""
    nonce = os.urandom(NONCE_LENGTH_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{FIELD_PREFIX}:{FIELD_VERSION}:{_b64u_encode(nonce)}:{_b64u_encode(ciphertext)}"


def decrypt_field(token: str, key: bytes) -> str:
    parts = token.split(":")
    if len(parts) != 4 or parts[0] != FIELD_PREFIX or parts[1] != FIELD_VERSION:
        raise DecryptionError("unrecognised encrypted-field format")
    nonce, ciphertext = _b64u_decode(parts[2]), _b64u_decode(parts[3])
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except InvalidTag as exc:
        raise DecryptionError("field authentication failed (wrong key or tampered)") from exc


def is_encrypted_field(value: str) -> bool:
    return value.startswith(f"{FIELD_PREFIX}:{FIELD_VERSION}:")


def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt a whole payload (e.g. a backup tar) into the file format."""
    nonce = os.urandom(NONCE_LENGTH_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return FILE_MAGIC + bytes([FILE_VERSION]) + nonce + ciphertext


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    header_len = len(FILE_MAGIC) + 1 + NONCE_LENGTH_BYTES
    if len(blob) < header_len or blob[: len(FILE_MAGIC)] != FILE_MAGIC:
        raise DecryptionError("not a BoxMedia backup archive")
    nonce = blob[len(FILE_MAGIC) + 1 : header_len]
    ciphertext = blob[header_len:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError("archive authentication failed (wrong key or tampered)") from exc


def _genkey(key_file: Path) -> int:
    import sys

    if key_file.exists():
        print(f"refusing to overwrite existing key file: {key_file}", file=sys.stderr)
        return 1
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(base64.urlsafe_b64encode(generate_key()))
    key_file.chmod(0o600)
    print(f"wrote new 256-bit key to {key_file} (mode 600)")
    return 0


def _rotate(old_key_file: Path, new_key_file: Path, data_dir: Path) -> int:
    """Re-encrypt every stored Radarr API key from the old key to the new one.

    Stop BoxMedia first: a running instance holds the old key in memory and would write
    old-key ciphertext back over the rotated file. Every key is decrypted and re-encrypted
    in memory before anything is written, so a single bad field aborts with the file
    untouched (all-or-nothing).
    """
    import sys

    from app.core import filestore

    try:
        old_key = load_key(old_key_file)
        new_key = load_key(new_key_file)
    except EncryptionKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    apps_path = data_dir.joinpath(*APPS_CONFIG_PATH)
    if not apps_path.exists():
        print(f"error: no connections file at {apps_path}", file=sys.stderr)
        return 1

    document = filestore.read_yaml(apps_path, expected_version=APPS_SCHEMA_VERSION)
    rotated: list[dict] = []
    for item in document.get(APPS_LIST_KEY, []):
        entry = dict(item)
        label = entry.get("name") or entry.get("id") or "?"
        try:
            plaintext = decrypt_field(entry[APPS_KEY_FIELD], old_key)
        except (DecryptionError, KeyError) as exc:
            print(
                f"error: could not decrypt the API key for {label!r} with {old_key_file}: "
                f"{exc}\nnothing was written — {apps_path} is unchanged.",
                file=sys.stderr,
            )
            return 1
        entry[APPS_KEY_FIELD] = encrypt_field(plaintext, new_key)
        rotated.append(entry)

    filestore.write_yaml(
        apps_path, {APPS_LIST_KEY: rotated}, schema_version=APPS_SCHEMA_VERSION
    )
    print(f"re-encrypted {len(rotated)} Radarr API key(s) in {apps_path}")
    print(f"next: point BM_ENCRYPTION_KEY_FILE at {new_key_file} and start BoxMedia again.")
    print(
        "keep the old key until every backup taken with it is gone — existing .backup "
        "archives can only be decrypted with the key they were created under."
    )
    return 0


def _main(argv: list[str]) -> int:
    """CLI entry point: `python -m app.core.crypto <genkey|rotate> ...`."""
    import sys

    if argv[:1] == ["genkey"] and len(argv) == 2:
        return _genkey(Path(argv[1]))
    if argv[:1] == ["rotate"] and len(argv) == 4:
        return _rotate(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
