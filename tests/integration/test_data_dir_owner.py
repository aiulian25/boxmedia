"""One process per data directory, and a second one says so instead of overwriting."""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import filestore
from app.core.filestore import DataDirInUseError

REPO_ROOT = Path(__file__).resolve().parents[2]
ALREADY_EXITED = 0
EXPECTED_MESSAGE = "exactly one"
SESSION_SECRET = "s" * 40


def _second_process(script: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — this interpreter, a script written above
        [sys.executable, "-c", script, str(data_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )


CLAIM_SCRIPT = (
    "import sys; from pathlib import Path; from app.core import filestore; "
    "filestore.claim_data_dir(Path(sys.argv[1])); print('claimed')"
)


def test_a_second_process_is_refused_while_the_first_lives(tmp_path: Path) -> None:
    filestore.claim_data_dir(tmp_path)

    second = _second_process(CLAIM_SCRIPT, tmp_path)

    assert second.returncode != ALREADY_EXITED
    assert EXPECTED_MESSAGE in second.stderr


def test_a_second_app_stops_before_it_can_repair_or_create_anything(tmp_path: Path) -> None:
    """Recovery would otherwise roll back a restore the first process was in the middle of,
    and a missing account would be bootstrapped."""
    key_file = tmp_path / "k.key"
    key_file.write_bytes(b"not read: the claim comes first")
    data_dir = tmp_path / "data"
    filestore.claim_data_dir(data_dir)
    script = (
        "import sys; from pathlib import Path; from app.main import create_app; "
        "from app.core.config import Settings; "
        f"create_app(Settings(_env_file=None, session_secret='{SESSION_SECRET}', "
        f"encryption_key_file=Path('{key_file}'), data_dir=Path(sys.argv[1])))"
    )

    second = _second_process(script, data_dir)

    assert second.returncode != ALREADY_EXITED
    assert EXPECTED_MESSAGE in second.stderr
    assert not (data_dir / "config").exists()


def test_the_claim_ends_with_the_process_that_held_it(tmp_path: Path) -> None:
    """A crash must not leave an install locked out of its own data: a stale lock file with
    nobody holding it means nothing."""
    assert _second_process(CLAIM_SCRIPT, tmp_path).stdout.strip() == "claimed"
    assert (tmp_path / filestore.OWNER_LOCK_FILENAME).exists()

    assert _second_process(CLAIM_SCRIPT, tmp_path).stdout.strip() == "claimed"


def test_two_directories_do_not_conflict(tmp_path: Path) -> None:
    filestore.claim_data_dir(tmp_path / "one")

    assert _second_process(CLAIM_SCRIPT, tmp_path / "two").stdout.strip() == "claimed"


def test_the_same_process_claiming_again_is_the_same_owner(tmp_path: Path) -> None:
    filestore.claim_data_dir(tmp_path)
    filestore.claim_data_dir(tmp_path)


def test_a_mount_without_file_locks_does_not_stop_a_single_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*_: object) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", unsupported)

    filestore.claim_data_dir(tmp_path)


def test_a_conflict_in_this_process_is_the_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def held(*_: object) -> None:
        raise BlockingIOError(errno.EWOULDBLOCK, "held")

    monkeypatch.setattr(fcntl, "flock", held)

    with pytest.raises(DataDirInUseError):
        filestore.claim_data_dir(tmp_path)


def test_an_unwritable_directory_is_left_for_the_better_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Settings.ensure_data_dirs` explains this one; a lock-file error would not."""

    def refused(*_: object, **__: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "open", refused)

    filestore.claim_data_dir(tmp_path)
