"""The one-shot init that makes a fresh bind mount usable by the unprivileged app.

Chown needs root, which tests do not have, so `os.chown` is stubbed and the calls are
recorded. What matters here is the DECISIONS — whether a key is generated, whether the
recursive pass runs, whether a lost key stops the start — not the syscall.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from app.core import provision
from app.core.crypto import KEY_LENGTH_BYTES, load_key


@pytest.fixture
def owned(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Records every chown, since a test process cannot make a real one."""
    calls: list[Path] = []

    def _chown(path, uid: int, gid: int) -> None:  # noqa: ANN001
        assert (uid, gid) == (provision.APP_UID, provision.APP_GID)
        calls.append(Path(path))

    monkeypatch.setattr(provision.os, "chown", _chown)
    return calls


def test_the_app_uid_matches_the_image_it_runs_in() -> None:
    """65532 is distroless's `nonroot`, written out in the Dockerfile's USER line. If one
    moves without the other, the app lands on a directory it cannot write."""
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    assert f"USER {provision.APP_UID}:{provision.APP_GID}" in dockerfile.read_text()


def test_a_missing_data_dir_is_created_and_handed_over(tmp_path: Path, owned) -> None:
    data = tmp_path / "data"

    assert provision.prepare_data_dir(data) is True
    assert data.is_dir()
    assert data in owned


def test_existing_content_is_handed_over_too(tmp_path: Path, owned) -> None:
    """The first run is not the only root-owned case: a restore that copied files back
    as root leaves the same trap one level down."""
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "user.yml").write_text("x", encoding="utf-8")

    provision.prepare_data_dir(data)

    assert {path.name for path in owned} >= {"data", "config", "user.yml"}


def test_a_directory_already_owned_by_the_app_is_left_alone(
    tmp_path: Path, owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steady state. A recursive chown every start would grow with the poster cache,
    which is the one directory here that keeps growing."""
    data = tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    # The directory really is owned by this process, so the check runs against a real
    # stat rather than a stub that would also have to fake mkdir's own probing.
    monkeypatch.setattr(provision, "APP_UID", os.getuid())

    assert provision.prepare_data_dir(data) is False
    assert owned == []


def test_a_first_run_gets_a_usable_key(tmp_path: Path, owned) -> None:
    key = tmp_path / "secrets" / "boxmedia.key"

    assert provision.prepare_key(key, tmp_path / "data") is True
    assert len(load_key(key)) == KEY_LENGTH_BYTES  # the app's own loader accepts it
    assert key.stat().st_mode & 0o777 == provision.KEY_MODE
    assert key in owned


def test_an_existing_key_is_never_touched(tmp_path: Path, owned) -> None:
    key = tmp_path / "secrets" / "boxmedia.key"
    key.parent.mkdir(parents=True)
    key.write_bytes(base64.urlsafe_b64encode(b"k" * KEY_LENGTH_BYTES))
    before = key.read_bytes()

    assert provision.prepare_key(key, tmp_path / "data") is False
    assert key.read_bytes() == before


@pytest.mark.parametrize("marker", provision.EXISTING_INSTALL_MARKERS)
def test_a_lost_key_stops_the_start_rather_than_making_a_new_one(
    tmp_path: Path, owned, marker: str
) -> None:
    """The failure this exists to prevent: a fresh key against existing data turns
    recoverable ciphertext into permanent noise, silently, on the next start."""
    data = tmp_path / "data"
    (data / marker).parent.mkdir(parents=True, exist_ok=True)
    (data / marker).write_text("x", encoding="utf-8")
    key = tmp_path / "secrets" / "boxmedia.key"

    with pytest.raises(SystemExit) as raised:
        provision.prepare_key(key, data)

    assert "Restore it from your backup" in str(raised.value)
    assert not key.exists(), "a key was written despite the refusal"


def test_the_markers_are_files_an_install_really_writes() -> None:
    """Pinned against the stores that own them, so a renamed file cannot silently turn
    every existing install into a 'first run'."""
    from app.services.apps import APPS_FILENAME
    from app.services.users import USER_FILENAME

    assert f"config/{APPS_FILENAME}" in provision.EXISTING_INSTALL_MARKERS
    assert f"config/{USER_FILENAME}" in provision.EXISTING_INSTALL_MARKERS


def test_a_key_path_docker_turned_into_a_directory_is_cleared(
    tmp_path: Path, owned
) -> None:
    """A file bind-mount whose source does not exist makes Docker create a DIRECTORY of
    that name — which is what the first published compose file did to boxmedia.key."""
    key = tmp_path / "secrets" / "boxmedia.key"
    key.mkdir(parents=True)

    assert provision.prepare_key(key, tmp_path / "data") is True
    assert key.is_file()
