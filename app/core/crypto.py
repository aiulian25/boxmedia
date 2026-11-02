"""AES-256-GCM encryption for every stored credential, and for backup archives.

Chosen over age/gpg because the distroless runtime has no shell or package
manager to invoke external binaries (Step 4 rationale). The key is loaded from
`BM_ENCRYPTION_KEY_FILE`, which lives outside the data directory so a backup of
`/data` can never contain the key that decrypts it.

Three stores hold ciphertext under that key — the Radarr/Sonarr API keys, the
media-server token, and the discovery credentials — and `rotate` moves all three
together. It is safe to interrupt: GCM proves which key a field is under, so re-running
the same command finishes the job from wherever it stopped.

Field format:  gcm:v1:<b64url(nonce)>:<b64url(ciphertext+tag)>
File format:   4-byte magic | 1-byte version | 12-byte nonce | ciphertext+tag
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LENGTH_BYTES = 32  # AES-256
NONCE_LENGTH_BYTES = 12  # GCM standard nonce
FIELD_PREFIX = "gcm"
FIELD_VERSION = "v1"
FILE_MAGIC = b"BMB1"  # BoxMedia Backup, format 1
FILE_VERSION = 1
# Version 2 encrypts in chunks, so an archive of any size needs one chunk of memory. Version 1,
# one AES-GCM block over the whole file, needs all of it, and stays readable.
FILE_VERSION_CHUNKED = 2
CHUNK_PLAINTEXT_BYTES = 1024 * 1024
_CHUNK_NONCE_PREFIX_BYTES = 7
_CHUNK_COUNTER_BYTES = 4
_CHUNK_LENGTH_BYTES = 4
_FINAL_CHUNK = b"\x01"
_NOT_FINAL_CHUNK = b"\x00"
GCM_TAG_BYTES = 16
_READ_BYTES = 1024 * 1024

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
# An interrupted restore's journal, mirrored from app.services.maintenance. While it exists
# config/ may be about to be swapped back, which would undo a rotation for that half only.
RESTORE_JOURNAL_FILENAME = ".restore-journal.json"

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
    "the discovery keys. Stop BoxMedia first. If it is interrupted, run it again."
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


def _chunk_nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    """Prefix, position, and whether this is the end. Authenticating the position and the end
    in every chunk means chunks cannot be reordered, dropped or cut short unnoticed."""
    position = counter.to_bytes(_CHUNK_COUNTER_BYTES, "big")
    return prefix + position + (_FINAL_CHUNK if final else _NOT_FINAL_CHUNK)


class ChunkedWriter:
    """Encrypts what is written to it into the version 2 file format, one chunk at a time."""

    def __init__(self, sink: BinaryIO, key: bytes) -> None:
        self._sink = sink
        self._aead = AESGCM(key)
        self._prefix = os.urandom(_CHUNK_NONCE_PREFIX_BYTES)
        self._header = FILE_MAGIC + bytes([FILE_VERSION_CHUNKED]) + self._prefix
        self._counter = 0
        self._pending = bytearray()
        sink.write(self._header)

    def write(self, data: bytes) -> int:
        self._pending += data
        while len(self._pending) > CHUNK_PLAINTEXT_BYTES:
            self._emit(bytes(self._pending[:CHUNK_PLAINTEXT_BYTES]), final=False)
            del self._pending[:CHUNK_PLAINTEXT_BYTES]
        return len(data)

    def finish(self) -> None:
        """Write the last chunk, which always exists, even empty: it is what proves the end."""
        self._emit(bytes(self._pending), final=True)
        self._pending.clear()

    def _emit(self, chunk: bytes, *, final: bool) -> None:
        nonce = _chunk_nonce(self._prefix, self._counter, final)
        sealed = self._aead.encrypt(nonce, chunk, self._header)
        self._sink.write(len(sealed).to_bytes(_CHUNK_LENGTH_BYTES, "big") + sealed)
        self._counter += 1


class ChunkedReader:
    """Reads the version 2 format as plaintext, authenticating each chunk before handing it on.

    A reader that stops early has not proved the file's end, so a caller parsing it must read
    to the end before trusting the whole.
    """

    def __init__(self, source: BinaryIO, key: bytes) -> None:
        self._source = source
        self._aead = AESGCM(key)
        self._header = source.read(len(FILE_MAGIC) + 1 + _CHUNK_NONCE_PREFIX_BYTES)
        expected_start = FILE_MAGIC + bytes([FILE_VERSION_CHUNKED])
        full_length = len(expected_start) + _CHUNK_NONCE_PREFIX_BYTES
        if len(self._header) != full_length or not self._header.startswith(expected_start):
            raise DecryptionError("not a chunked BoxMedia backup archive")
        self._prefix = self._header[len(expected_start) :]
        self._counter = 0
        self._next_frame = self._read_frame()
        self._plain = bytearray()
        self._ended = False

    def _read_frame(self) -> bytes | None:
        length_bytes = self._source.read(_CHUNK_LENGTH_BYTES)
        if not length_bytes:
            return None
        length = int.from_bytes(length_bytes, "big")
        if (
            len(length_bytes) != _CHUNK_LENGTH_BYTES
            or length > CHUNK_PLAINTEXT_BYTES + GCM_TAG_BYTES
        ):
            raise DecryptionError("archive authentication failed (malformed chunk)")
        frame = self._source.read(length)
        if len(frame) != length:
            raise DecryptionError("archive authentication failed (cut short)")
        return frame

    def _open_next_chunk(self) -> None:
        frame = self._next_frame
        if frame is None:
            raise DecryptionError("archive authentication failed (no end marker)")
        self._next_frame = self._read_frame()
        final = self._next_frame is None
        nonce = _chunk_nonce(self._prefix, self._counter, final)
        try:
            self._plain += self._aead.decrypt(nonce, frame, self._header)
        except InvalidTag as exc:
            raise DecryptionError(
                "archive authentication failed (wrong key or tampered)"
            ) from exc
        self._counter += 1
        self._ended = final

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._plain) < size) and not self._ended:
            self._open_next_chunk()
        taken = len(self._plain) if size < 0 else min(size, len(self._plain))
        data = bytes(self._plain[:taken])
        del self._plain[:taken]
        return data


def archive_version(path: Path) -> int:
    """Which file format an archive is in, from its header alone."""
    with path.open("rb") as source:
        header = source.read(len(FILE_MAGIC) + 1)
    if len(header) != len(FILE_MAGIC) + 1 or not header.startswith(FILE_MAGIC):
        raise DecryptionError("not a BoxMedia backup archive")
    return header[-1]


def decrypt_single_block_file(path: Path, sink: BinaryIO, key: bytes) -> None:
    """Decrypt a version 1 archive to `sink` without holding it in memory.

    Authenticated only when the end is reached, so the caller must not read `sink` until this
    returns, and must discard it if this raises.
    """
    header_length = len(FILE_MAGIC) + 1 + NONCE_LENGTH_BYTES
    remaining = path.stat().st_size - header_length - GCM_TAG_BYTES
    if remaining < 0:
        raise DecryptionError("not a BoxMedia backup archive")
    with path.open("rb") as source:
        nonce = source.read(header_length)[-NONCE_LENGTH_BYTES:]
        source.seek(-GCM_TAG_BYTES, os.SEEK_END)
        tag = source.read(GCM_TAG_BYTES)
        source.seek(header_length)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        while remaining:
            block = source.read(min(_READ_BYTES, remaining))
            remaining -= len(block)
            sink.write(decryptor.update(block))
    try:
        sink.write(decryptor.finalize())
    except InvalidTag as exc:
        raise DecryptionError("archive authentication failed (wrong key or tampered)") from exc


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    header_len = len(FILE_MAGIC) + 1 + NONCE_LENGTH_BYTES
    if len(blob) < header_len or blob[: len(FILE_MAGIC)] != FILE_MAGIC:
        raise DecryptionError("not a BoxMedia backup archive")
    if blob[len(FILE_MAGIC)] == FILE_VERSION_CHUNKED:
        return ChunkedReader(io.BytesIO(blob), key).read()
    nonce = blob[len(FILE_MAGIC) + 1 : header_len]
    # A view, not a slice: a slice would hold a second copy of a whole archive in memory,
    # alongside the blob and the plaintext, at the moment an import is at its largest.
    ciphertext = memoryview(blob)[header_len:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError("archive authentication failed (wrong key or tampered)") from exc


def _genkey(key_file: Path) -> int:
    import sys

    from app.core import filestore

    if key_file.exists():
        print(f"refusing to overwrite existing key file: {key_file}", file=sys.stderr)
        return 1
    try:
        # Atomic and synced, because ciphertext is about to depend on this file: a key lost
        # to a power cut after a rotation is every credential lost with it.
        filestore.atomic_write_bytes(key_file, base64.urlsafe_b64encode(generate_key()))
        key_file.chmod(0o600)
    except PermissionError as exc:
        print(
            f"error: cannot create {key_file}: {exc}\n"
            "  The directory must be writable by the user running this (uid 65532 in the\n"
            "  image). Start the stack once with the current docker-compose.yml, whose init\n"
            "  service hands it over, then run this again.",
            file=sys.stderr,
        )
        return 1
    print(f"wrote new 256-bit key to {key_file} (mode 600)")
    return 0


# One file's rewritten contents, held until every store has decrypted: the path, the
# document to write, and the schema version to stamp it with.
_PendingWrite = tuple[Path, dict, int]


class _RotationAborted(Exception):
    """A stored field would not decrypt, so no file may be written.

    Carries the sentence naming which credential failed; `_rotate` prints it and stops.
    """


def _opens_with(token: str, key: bytes) -> bool:
    try:
        decrypt_field(token, key)
    except DecryptionError:
        return False
    return True


def _rotated_field(token: object, label: str, old_key: bytes, new_key: bytes) -> str:
    """One credential under the new key, or an abort naming it.

    Already under the new key is success, not failure, and GCM's tag is what makes that safe
    to conclude. This is the whole of resuming: an interrupted run left each field under
    one key or the other, so running again finishes it, and swapping the two keys undoes it.
    """
    if not isinstance(token, str):
        raise _RotationAborted(f"{label} is missing or is not an encrypted field")
    try:
        plaintext = decrypt_field(token, old_key)
    except DecryptionError as exc:
        if _opens_with(token, new_key):
            return token
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


def _sync_file(path: Path) -> None:
    from app.core import filestore

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    filestore.fsync_directory(path.parent)


def _rotate(old_key_file: Path, new_key_file: Path, data_dir: Path) -> int:
    """Re-encrypt every stored credential from the old key to the new one.

    Stop BoxMedia first: a running instance holds the old key in memory and would write
    old-key ciphertext back over the rotated files.

    Every field in all three stores is decrypted and re-encrypted in memory before ANY
    file is written, so one field neither key opens aborts with every file untouched.

    The writes are atomic per file but not as a group, so a crash or a full disk can leave
    some stores under each key. No journal is needed to get out of that: see
    `_rotated_field`. Re-running the same command finishes the rotation, and swapping the
    two keys undoes it. Either way both key files must survive, so the new one is made
    durable before anything starts to depend on it.
    """
    import sys

    from app.core import filestore

    try:
        old_key = load_key(old_key_file)
        new_key = load_key(new_key_file)
    except EncryptionKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if (data_dir / RESTORE_JOURNAL_FILENAME).exists():
        print(
            f"error: {data_dir} holds an interrupted restore. Start BoxMedia once so it can "
            "recover, stop it, then rotate.",
            file=sys.stderr,
        )
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

    written = 0
    try:
        _sync_file(new_key_file)
        for path, document, schema_version in pending:
            filestore.write_yaml(path, document, schema_version=schema_version)
            written += 1
    except OSError as exc:
        print(
            f"error: rotation stopped after {written} of {len(pending)} file(s): {exc}\n"
            "  Some credentials may now be under each key. KEEP BOTH KEY FILES, fix the\n"
            "  problem and run this same command again: it finishes from where it stopped.\n"
            "  To go back to the old key instead, run it with the two keys swapped.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(rotated_apps)} connection API key(s) in {apps_path} are under the new key")
    for path, _document, _schema_version in pending:
        if path != apps_path:
            print(f"the stored credentials in {path} are under the new key")
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
