"""Radarr defaults and the weekly-check schedule (Step 13).

Holds the quality profile and root folder a manually-added title uses, plus how
often the pipeline fetches the box-office chart. BoxMedia never auto-adds — adding
is a deliberate, per-title action on the weekly view — so the old genre/rating/year
"auto-add filters" and folder routing were removed. Old `filters.yml` files that
still carry those keys load fine: unknown fields are ignored.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from app.core import filestore
from app.core.audit import AuditAction, AuditLog
from app.services import backup
from app.services.boxoffice import (
    DOMESTIC_REGION,
    MAX_CHART_SIZE,
    MIN_CHART_SIZE,
    TOP_N,
    validated_region,
)
from app.services.reports import MAX_REPORT_RETENTION, MAX_REPORTS, MIN_REPORT_RETENTION

FILTERS_SCHEMA_VERSION = 1
FILTERS_FILENAME = "filters.yml"
DEFAULT_SCHEDULE_INTERVAL_HOURS = 168  # weekly
# How the unattended check is timed. The config owns its vocabulary, the way users.py owns
# THEMES: the scheduler owns WHEN each mode fires, and this owns which modes exist.
SCHEDULE_MODE_CADENCE = "mojo_cadence"
SCHEDULE_MODE_INTERVAL = "interval"
SCHEDULE_MODES = frozenset({SCHEDULE_MODE_CADENCE, SCHEDULE_MODE_INTERVAL})
DEFAULT_BACKUP_INTERVAL_DAYS = 0  # unattended backups are opt-in
# Mirrors app.services.backup.DEFAULT_KEEP; imported there so the two can't drift.
DEFAULT_BACKUP_KEEP = backup.DEFAULT_KEEP
# Same rule for the chart depth: boxoffice owns the number and its politeness bounds.
DEFAULT_CHART_SIZE = TOP_N
# And for history retention: reports owns the cap it prunes to, and its bounds.
DEFAULT_REPORT_KEEP = MAX_REPORTS
MIN_REPORT_KEEP = MIN_REPORT_RETENTION
MAX_REPORT_KEEP = MAX_REPORT_RETENTION


class FiltersConfig(BaseModel):
    quality_profile_id: int | None = None
    default_root_folder: str | None = None
    schedule_interval_hours: int = Field(default=DEFAULT_SCHEDULE_INTERVAL_HOURS, ge=1)
    # Unattended encrypted snapshots. 0 days = off; retention bounds the disk they use.
    backup_interval_days: int = Field(default=DEFAULT_BACKUP_INTERVAL_DAYS, ge=0)
    backup_keep: int = Field(default=DEFAULT_BACKUP_KEEP, ge=1)
    # How many chart positions a run records. Bounded, not free-form: the ceiling keeps
    # one run from hammering Box Office Mojo and Radarr.
    chart_size: int = Field(default=DEFAULT_CHART_SIZE, ge=MIN_CHART_SIZE, le=MAX_CHART_SIZE)
    # How many weekly reports are kept. Bounded at both ends: too few makes the month
    # leaderboard and the trend lines thin, too many makes every page read a history
    # nobody is looking at.
    report_keep: int = Field(
        default=DEFAULT_REPORT_KEEP, ge=MIN_REPORT_KEEP, le=MAX_REPORT_KEEP
    )
    # Which Box Office Mojo chart a run fetches. Empty is Domestic, which is what every
    # install had before this existed and what a file without the key still loads as.
    # Box Office Mojo publishes a week in stages — estimates, then actuals, then finals —
    # so following that rhythm records the finished figures rather than whichever stage a
    # single fire happened to catch. The interval below stays stored either way, so
    # switching back to it restores exactly what was configured.
    schedule_mode: str = SCHEDULE_MODE_CADENCE
    boxoffice_region: str = DOMESTIC_REGION

    @field_validator("schedule_mode", mode="before")
    @classmethod
    def _known_mode(cls, value: object) -> str:
        """Read-tolerant, like the region beside it: a file naming a mode this build does
        not ship starts on the cadence rather than refusing to load."""
        return value if value in SCHEDULE_MODES else SCHEDULE_MODE_CADENCE

    @field_validator("boxoffice_region", mode="before")
    @classmethod
    def _known_region(cls, value: object) -> str:
        """Read-tolerant: a hand-edited file naming a region this build does not ship
        loads as Domestic rather than refusing to start. The Settings route is the strict
        half — it refuses the same value outright."""
        return validated_region(value)


class FiltersStore:
    def __init__(self, config_dir: Path, *, audit: AuditLog) -> None:
        self._path = config_dir / FILTERS_FILENAME
        self._audit = audit

    def load(self) -> FiltersConfig:
        if not self._path.exists():
            return FiltersConfig()
        document = filestore.read_yaml(self._path, expected_version=FILTERS_SCHEMA_VERSION)
        document.pop(filestore.SCHEMA_VERSION_KEY, None)
        return FiltersConfig.model_validate(document)

    def save(self, config: FiltersConfig) -> None:
        filestore.write_yaml(
            self._path, config.model_dump(), schema_version=FILTERS_SCHEMA_VERSION
        )
        self._audit.record(AuditAction.FILTERS_UPDATED)
