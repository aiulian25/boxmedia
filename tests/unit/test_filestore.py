"""Step 3 test: round-trip, crash-safety of atomic rename, version stamping."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from app.core import filestore

SCHEMA_V1 = 1


def test_yaml_round_trip_with_version_stamp(tmp_path: Path) -> None:
    target = tmp_path / "settings.yml"
    filestore.write_yaml(target, {"name": "Radarr", "port": 7878}, schema_version=SCHEMA_V1)
    data = filestore.read_yaml(target, expected_version=SCHEMA_V1)
    assert data["name"] == "Radarr"
    assert data[filestore.SCHEMA_VERSION_KEY] == SCHEMA_V1


def test_json_round_trip_with_version_stamp(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    filestore.write_json(target, {"totals": {"movies": 10}}, schema_version=SCHEMA_V1)
    data = filestore.read_json(target, expected_version=SCHEMA_V1)
    assert data["totals"]["movies"] == 10
    assert data[filestore.SCHEMA_VERSION_KEY] == SCHEMA_V1


def test_crash_between_temp_and_rename_leaves_original_intact(tmp_path: Path) -> None:
    target = tmp_path / "settings.yml"
    filestore.write_yaml(target, {"value": "original"}, schema_version=SCHEMA_V1)

    # Simulate a crash after the temp file is written but before the rename lands.
    with mock.patch("app.core.filestore.os.replace", side_effect=OSError("power cut")):
        with pytest.raises(OSError):
            filestore.write_yaml(target, {"value": "new"}, schema_version=SCHEMA_V1)

    # Original content survived; no torn file, no leftover temp file.
    assert filestore.read_yaml(target, expected_version=SCHEMA_V1)["value"] == "original"
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".settings.yml.")]
    assert leftover == []


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "settings.yml"
    filestore.write_yaml(target, {"value": "x"}, schema_version=5)
    with pytest.raises(filestore.SchemaVersionError):
        filestore.read_yaml(target, expected_version=1)


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.yml"
    target.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        filestore.read_yaml(target, expected_version=SCHEMA_V1)


# --- dir_size_bytes tolerates concurrent deletion (review step 9) ---


def test_dir_size_bytes_totals_regular_files(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x" * 100)
    (tmp_path / "b.jpg").write_bytes(b"x" * 250)
    (tmp_path / "nested").mkdir()  # flat by design — a subdirectory contributes nothing
    (tmp_path / "nested" / "deep.jpg").write_bytes(b"x" * 9999)

    assert filestore.dir_size_bytes(tmp_path) == 350


def test_dir_size_bytes_is_zero_for_a_missing_directory(tmp_path: Path) -> None:
    assert filestore.dir_size_bytes(tmp_path / "never-created") == 0


def test_dir_size_bytes_is_zero_for_a_file(tmp_path: Path) -> None:
    target = tmp_path / "a-file.txt"
    target.write_text("not a directory", encoding="utf-8")
    assert filestore.dir_size_bytes(target) == 0


def test_a_file_deleted_between_listing_and_stat_is_skipped(tmp_path: Path) -> None:
    """The Storage card walks these directories on every render while the scheduled
    backup prunes them, so a listed file can be gone by the time it is measured — which
    used to 500 the page."""
    (tmp_path / "survivor.jpg").write_bytes(b"x" * 100)
    doomed = tmp_path / "doomed.jpg"
    doomed.write_bytes(b"x" * 500)

    real_stat = Path.stat
    deleted = False

    def stat_after_deleting(self: Path, *args: object, **kwargs: object) -> object:
        # A flag, not `doomed.exists()`: exists() itself calls Path.stat, so checking
        # the filesystem from inside the patch recurses forever.
        nonlocal deleted
        if self.name == doomed.name and not deleted:
            deleted = True
            doomed.unlink()  # pruned in the instant before it is measured
        return real_stat(self, *args, **kwargs)

    with mock.patch.object(Path, "stat", stat_after_deleting):
        total = filestore.dir_size_bytes(tmp_path)

    assert total == 100  # the survivor only; the pruned file costs nothing


def test_a_directory_removed_after_the_existence_check_is_zero_not_a_crash(
    tmp_path: Path,
) -> None:
    """A restore renames history/ and cache/ wholesale while the Settings page is
    reading them, so the directory can disappear between the check and the listing —
    a window an inner-loop guard alone would leave open."""
    target = tmp_path / "vanishing"
    target.mkdir()
    (target / "a.jpg").write_bytes(b"x" * 100)

    real_iterdir = Path.iterdir

    def iterdir_after_removing(self: Path):  # noqa: ANN202
        if self.name == target.name and (target / "a.jpg").is_file():
            (target / "a.jpg").unlink()
            target.rmdir()  # gone between is_dir() and the listing
        return real_iterdir(self)

    with mock.patch.object(Path, "iterdir", iterdir_after_removing):
        assert filestore.dir_size_bytes(target) == 0
