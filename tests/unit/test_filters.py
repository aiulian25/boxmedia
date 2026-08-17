"""Filters store: Radarr defaults + schedule persist, round-trip, and stay
backward-compatible with old filters.yml files that carry removed keys."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import filestore
from app.core.audit import AuditLog
from app.services.filters import (
    DEFAULT_REPORT_KEEP,
    FILTERS_FILENAME,
    FILTERS_SCHEMA_VERSION,
    MAX_REPORT_KEEP,
    MIN_REPORT_KEEP,
    FiltersConfig,
    FiltersStore,
)
from app.services.reports import MAX_REPORTS


def _store(tmp_path: Path) -> FiltersStore:
    return FiltersStore(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"))


def test_store_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() == FiltersConfig()  # default when the file is absent
    config = FiltersConfig(
        quality_profile_id=4, default_root_folder="/movies", schedule_interval_hours=24
    )
    store.save(config)
    assert store.load() == config


def test_load_ignores_removed_filter_fields(tmp_path: Path) -> None:
    # A filters.yml written before the auto-add filters were removed still loads:
    # the genre/rating/year keys are simply ignored, the live fields survive.
    filestore.write_yaml(
        tmp_path / FILTERS_FILENAME,
        {
            "exclude_genres": ["Horror"],
            "min_rating": 6.5,
            "min_year": 2020,
            "folder_routes": [{"genre": "Action", "weight": 5, "root_folder": "/a"}],
            "quality_profile_id": 7,
            "default_root_folder": "/movies",
            "schedule_interval_hours": 48,
        },
        schema_version=FILTERS_SCHEMA_VERSION,
    )
    config = _store(tmp_path).load()
    assert config.quality_profile_id == 7
    assert config.default_root_folder == "/movies"
    assert config.schedule_interval_hours == 48


# --- history retention is bounded at both ends ---


def test_report_retention_defaults_to_what_the_store_prunes_to() -> None:
    """The store owns the number; filters mirrors it, the way it already mirrors the
    backup retention and the chart depth — so the default and the code that applies it
    cannot drift apart."""
    assert FiltersConfig().report_keep == MAX_REPORTS
    assert DEFAULT_REPORT_KEEP == MAX_REPORTS


def test_report_retention_has_a_floor() -> None:
    """Below this the features built on the history stop meaning anything: the month
    leaderboard, the trend lines and "weeks tracked" all read it."""
    FiltersConfig(report_keep=MIN_REPORT_KEEP)
    with pytest.raises(ValidationError):
        FiltersConfig(report_keep=MIN_REPORT_KEEP - 1)


def test_report_retention_has_a_ceiling() -> None:
    """Retention is a page-load setting, not only a storage one — every dashboard and
    reports view reads the whole history directory (4ms at 50 reports, 17ms at 260,
    76ms at 1000)."""
    FiltersConfig(report_keep=MAX_REPORT_KEEP)
    with pytest.raises(ValidationError):
        FiltersConfig(report_keep=MAX_REPORT_KEEP + 1)
