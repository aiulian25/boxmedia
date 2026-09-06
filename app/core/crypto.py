"""AES-256-GCM encryption for every stored credential, and for backup archives.

Chosen over age/gpg because the distroless runtime has no shell or package
manager to invoke external binaries (Step 4 rationale). The key is loaded from
`BM_ENCRYPTION_KEY_FILE`, which lives outside the data directory so a backup of
`/data` can never contain the key that decrypts it.

Three stores hold ciphertext under that key — the Radarr/Sonarr API keys, the
media-server token, and the discovery credentials — and `rotate` moves all three
together. Rotating only some of them would leave an install half under each key,
and the half that moved could no longer be rotated back with the old one.

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

# The other two stores this key encrypts, mirrored for the same reason and pinned by the
# same test. Rotation reached only apps.yml until now, so these two stayed under the old
# key — and every page that read them raised instead of rendering.
MEDIA_SERVER_CONFIG_PATH = ("config", "mediaserver.yml")
# What 1.1.0 wrote, when Plex was the only kind. Rotated as well: an install that has not
# re-saved its connection since still reads this file, and skipping it would strand it.
LEGACY_PLEX_CONFIG_PATH = ("config", "plex.yml")
MEDIA_SERVER_SCHEMA_VERSION = 1
MEDIA_SERVER_KEY = "server"
MEDIA_SERVER_TOKEN_FIELD = "token_encrypted"  # noqa: S105 — field name, not a secret
DISCOVERY_CONFIG_PATH = ("config", "discovery.yml")
DISCOVERY_SCHEMA_VERSION = 1
DISCOVERY_FIELDS = ("tmdb_key_encrypted", "trakt_client_id_encrypted")
# Every stored credential field ends with it; stripped to name one in an error.
ENCRYPTED_FIELD_SUFFIX = "_encrypted"

# What a store says when its ciphertext will not open with the key this build is holding.
# One sentence in one place: three stores raise it as three different typed errors, and
# the advice is identical every time — the file and the key have come apart.
UNREADABLE_CREDENTIAL_MESSAGE = (
    "the stored credential cannot be decrypted with the current encryption key — "
    "restore the key file, or re-encrypt with `python -m app.core.crypto rotate`"
)

USAGE = (
    "usage:\n"
    "  python -m app.core.crypto genkey <key-file>\n"
    "  python -m app.core.crypto rotate <old-key-file> <new-key-file> <data-dir>\n"
    "\n"
    "rotate re-encrypts the stored Radarr/Sonarr API keys, the media-server token and\n"
    "the discovery keys. Stop BoxMedia first."
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


# One file's rewritten contents, held until every store has decrypted: the path, the
# document to write, and the schema version to stamp it with.
_PendingWrite = tuple[Path, dict, int]


class _RotationAborted(Exception):
    """A stored field would not decrypt, so no file may be written.

    Carries the sentence naming which credential failed; `_rotate` prints it and stops.
    """


def _rotated_field(token: object, label: str, old_key: bytes, new_key: bytes) -> str:
    """One credential moved from the old key to the new one, or an abort naming it."""
    if not isinstance(token, str):
        raise _RotationAborted(f"{label} is missing or is not an encrypted field")
    try:
        plaintext = decrypt_field(token, old_key)
    except DecryptionError as exc:
        raise _RotationAborted(f"could not decrypt {label}: {exc}") from exc
    return encrypt_field(plaintext, new_key)


def _rotated_apps(document: dict, old_key: bytes, new_key: bytes) -> list[dict]:
    """Every connection's API key, re-encrypted, in the order the file stores them."""
    rotated: list[dict] = []
    for item in document.get(APPS_LIST_KEY, []):
        entry = dict(item)
        label = entry.get("name") or entry.get("id") or "?"
        entry[APPS_KEY_FIELD] = _rotated_field(
            entry.get(APPS_KEY_FIELD), f"the API key for {label!r}", old_key, new_key
        )
        rotated.append(entry)
    return rotated


def _rotated_media_server(
    document: dict, source: str, old_key: bytes, new_key: bytes
) -> dict | None:
    """The stored connection with its token re-encrypted, or None when it holds none.

    None rather than an abort for a record with no token: that is a hand-edited or
    emptied file, not a credential this key failed to open.
    """
    server = document.get(MEDIA_SERVER_KEY)
    if not isinstance(server, dict) or not server.get(MEDIA_SERVER_TOKEN_FIELD):
        return None
    rotated = dict(server)
    rotated[MEDIA_SERVER_TOKEN_FIELD] = _rotated_field(
        server.get(MEDIA_SERVER_TOKEN_FIELD),
        f"the media-server token in {source}",
        old_key,
        new_key,
    )
    return rotated


def _rotated_discovery(
    document: dict, source: str, old_key: bytes, new_key: bytes
) -> dict:
    """The discovery document, with whichever of its two keys are stored re-encrypted."""
    rotated = dict(document)
    for field in DISCOVERY_FIELDS:
        if not rotated.get(field):
            continue
        credential = field.removesuffix(ENCRYPTED_FIELD_SUFFIX).replace("_", " ")
        rotated[field] = _rotated_field(
            rotated[field], f"the {credential} in {source}", old_key, new_key
        )
    return rotated


def _existing(data_dir: Path, *relative_paths: tuple[str, ...]) -> list[Path]:
    """Whichever of those files this install actually has."""
    candidates = (data_dir.joinpath(*relative) for relative in relative_paths)
    return [path for path in candidates if path.exists()]


def _rotate(old_key_file: Path, new_key_file: Path, data_dir: Path) -> int:
    """Re-encrypt every stored credential from the old key to the new one.

    Stop BoxMedia first: a running instance holds the old key in memory and would write
    old-key ciphertext back over the rotated files.

    Every field in all three stores is decrypted and re-encrypted in memory before ANY
    file is written, so one bad field aborts with every file untouched. See the module
    docstring for why a partial rotation is worse than none.

    The writes themselves are atomic per file but not as a group. The app is stopped for
    the whole operation and the files are small, so the window is a few milliseconds
    across three renames; a crash inside it is recovered by re-running with whichever key
    each remaining file still holds.
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

    pending: list[_PendingWrite] = []
    try:
        apps_document = filestore.read_yaml(
            apps_path, expected_version=APPS_SCHEMA_VERSION
        )
        rotated_apps = _rotated_apps(apps_document, old_key, new_key)
        pending.append((apps_path, {APPS_LIST_KEY: rotated_apps}, APPS_SCHEMA_VERSION))

        for path in _existing(
            data_dir, MEDIA_SERVER_CONFIG_PATH, LEGACY_PLEX_CONFIG_PATH
        ):
            document = filestore.read_yaml(
                path, expected_version=MEDIA_SERVER_SCHEMA_VERSION
            )
            server = _rotated_media_server(document, path.name, old_key, new_key)
            if server is None:
                continue
            pending.append(
                (path, {MEDIA_SERVER_KEY: server}, MEDIA_SERVER_SCHEMA_VERSION)
            )

        for path in _existing(data_dir, DISCOVERY_CONFIG_PATH):
            document = filestore.read_yaml(
                path, expected_version=DISCOVERY_SCHEMA_VERSION
            )
            document.pop(filestore.SCHEMA_VERSION_KEY, None)
            pending.append(
                (
                    path,
                    _rotated_discovery(document, path.name, old_key, new_key),
                    DISCOVERY_SCHEMA_VERSION,
                )
            )
    except (_RotationAborted, filestore.SchemaVersionError) as failure:
        print(
            f"error: {failure}\n"
            f"  old key: {old_key_file}\n"
            f"nothing was written — {data_dir} is unchanged.",
            file=sys.stderr,
        )
        return 1

    for path, document, schema_version in pending:
        filestore.write_yaml(path, document, schema_version=schema_version)

    print(f"re-encrypted {len(rotated_apps)} connection API key(s) in {apps_path}")
    for path, _document, _schema_version in pending:
        if path != apps_path:
            print(f"re-encrypted the stored credentials in {path}")
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
