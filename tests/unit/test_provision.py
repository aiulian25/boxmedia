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


# --- what the app says when nothing prepared its data directory ---


def test_an_unwritable_data_dir_explains_itself_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure, twice: a forty-line traceback ending in `os.mkdir` that
    names pathlib and blames nothing. It must name the directory, both uids, and the
    two things that actually cause it."""
    from app.core.config import Settings

    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)  # readable, not writable — what a root-owned mount looks like
    settings = Settings(
        _env_file=None, session_secret="s" * 40,
        encryption_key_file=tmp_path / "k.key", data_dir=data,
    )

    with pytest.raises(SystemExit) as raised:
        settings.ensure_data_dirs()

    message = str(raised.value)
    assert str(data) in message
    assert str(provision.APP_UID) in message
    assert "init" in message                    # the compose service that prepares it
    assert "BM_DATA_PATH" in message            # the desync cause
    assert "chown" in message                   # the manual escape hatch
    assert "Traceback" not in message


def _compose() -> dict:
    """The published compose file, with YAML anchors expanded — which the parser does
    natively, so this needs no Docker and runs anywhere the suite does. The `${VAR}`
    strings stay unexpanded, which is exactly right: two services agree when they name
    the same variable, whatever it resolves to on a given host."""
    import yaml

    path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _mount(service: dict, target: str) -> str:
    return next(entry for entry in service["volumes"] if entry.endswith(f":{target}"))


def test_the_compose_services_cannot_be_pointed_at_different_directories() -> None:
    """init preparing one path while the app mounts another fails IDENTICALLY to an
    unprepared install — a footgun this file's own fix introduced. However the YAML is
    written, both services must name the same data directory."""
    services = _compose()["services"]

    assert _mount(services["init"], "/data") == _mount(services["boxmedia"], "/data")
    # And no bare host path, which is how they drifted apart in the first place.
    assert "./data:/data" not in _mount(services["boxmedia"], "/data")


def test_both_services_run_the_same_build() -> None:
    """Two image references meant two places to bump by hand every release. An app
    running against an init from another version is a failure nobody would look for."""
    services = _compose()["services"]

    assert services["init"]["image"] == services["boxmedia"]["image"]


def test_only_the_init_may_write_the_key() -> None:
    """The key is created once and never touched again. Read-only on the app is what
    stops a compromised app rewriting the thing that decrypts its own backups."""
    services = _compose()["services"]

    assert _mount(services["init"], "/secrets").endswith(":/secrets")
    assert _mount(services["boxmedia"], "/secrets:ro").endswith(":ro")
