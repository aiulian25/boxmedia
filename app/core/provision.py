"""Prepare a fresh bind mount so the unprivileged app can actually use it.

Run once, as root, by the `init` service in docker-compose.yml — never by the app,
which runs as uid 65532 on a read-only filesystem and therefore cannot do any of this
for itself. That is the whole problem: Docker creates a missing bind-mount source as
`root:root`, so the very first `docker compose up -d` on a new host died with
`PermissionError: /data/config` before the app printed a single useful line.

Two jobs, both idempotent:

* Hand `/data` to the app's uid, so it can create its own subdirectories.
* Put an encryption key at `/secrets/boxmedia.key` when there is none — but ONLY on a
  genuinely new install. A key missing from an install that already has data is not a
  first run, it is a lost key, and generating a fresh one there would turn recoverable
  ciphertext into permanent noise. That case stops with an error naming the backup.

Both live here, in the image, rather than as a shell one-liner in the compose file:
distroless has no shell, and this is logic worth testing.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
from pathlib import Path

from app.core.crypto import generate_key

# The distroless "nonroot" user the runtime stage runs as. Duplicated from the Dockerfile
# because a container cannot read its own image config; the test below pins the two equal.
APP_UID = 65532
APP_GID = 65532

# Read from the environment the app itself uses, so a rotation that moved the key to a
# new filename does not look like a missing one to the check below.
DATA_DIR = Path(os.environ.get("BM_DATA_DIR", "/data"))
KEY_FILE = Path(os.environ.get("BM_ENCRYPTION_KEY_FILE", "/secrets/boxmedia.key"))
# What proves an install is not new. The apps file holds the encrypted Radarr keys — the
# exact thing a regenerated encryption key would render unreadable.
EXISTING_INSTALL_MARKERS = ("config/apps.yml", "config/user.yml")

KEY_MODE = 0o600


def _own(path: Path) -> None:
    os.chown(path, APP_UID, APP_GID)


def prepare_data_dir(data_dir: Path = DATA_DIR) -> bool:
    """Make `data_dir` writable by the app. True when something changed.

    Recurses only when the top level is wrong, which is the first run and any restore
    that copied files back as root — never the steady state, where the poster cache
    would make an unconditional recursive chown a per-start cost that grows with use.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir.stat().st_uid == APP_UID:
        return False
    _own(data_dir)
    for child in data_dir.rglob("*"):
        _own(child)
    return True


def has_existing_install(data_dir: Path = DATA_DIR) -> bool:
    """Whether this data directory already holds an install's own records."""
    return any((data_dir / marker).exists() for marker in EXISTING_INSTALL_MARKERS)


def prepare_key(key_file: Path = KEY_FILE, data_dir: Path = DATA_DIR) -> bool:
    """Ensure an encryption key exists. True when one was generated.

    Raises when the key is missing but the data directory is not: see the module
    docstring — that is a lost key, and the only safe move is to stop and say so.
    """
    # Docker creates a missing bind-mount SOURCE as a directory, so a compose file that
    # mounted the key as a file left `boxmedia.key/` behind on the host. The public
    # compose mounts the directory now; this clears the wreckage of the older one.
    if key_file.is_dir():
        shutil.rmtree(key_file)

    if key_file.exists():
        return False
    if has_existing_install(data_dir):
        raise SystemExit(
            f"error: {key_file} is missing but {data_dir} already holds an install.\n"
            "  That key is the only thing that can decrypt the stored Radarr API keys\n"
            "  and any backup archive. Restore it from your backup rather than starting\n"
            "  fresh — a new key here would make that data permanently unreadable."
        )

    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(base64.urlsafe_b64encode(generate_key()))
    key_file.chmod(KEY_MODE)
    _own(key_file)
    return True


def main() -> int:
    changed_data = prepare_data_dir()
    made_key = prepare_key()
    if made_key:
        # As loud as the first-run admin password, and for the same reason: it is the one
        # thing here nobody can reissue.
        print(
            "\n" + "=" * 70 + "\n"
            f"  BoxMedia generated a new encryption key at {KEY_FILE}.\n"
            "  BACK IT UP. It decrypts your stored Radarr API keys and every backup\n"
            "  archive; without it they cannot be recovered by anyone, including you.\n"
            + "=" * 70 + "\n",
            flush=True,
        )
    if changed_data:
        print(f"boxmedia-init: {DATA_DIR} handed to uid {APP_UID}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
