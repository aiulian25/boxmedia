"""What the unattended check last did, recorded apart from what it found.

A report cannot answer that. A check that finds an unchanged chart rightly writes nothing,
and a manual re-run takes over the week's report, trigger and all, so a schedule judged by its
reports could be firing faithfully and still read "never". That also misled the anchor a
restart resumes from, and re-queued a catch-up on every boot.

One small file, in config/ so a backup carries it, and with no chart data in it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from app.core import filestore
from app.services.boxoffice import FETCH_FAILED_PREFIX, NO_DATA_PREFIX
from app.services.reports import Report, RunStatus

INGESTION_FILENAME = "ingestion.yml"
INGESTION_SCHEMA_VERSION = 1

OUTCOME_CHANGED = "changed"  # the chart differed, and was stored
OUTCOME_UNCHANGED = "unchanged"  # the chart was what is already stored
OUTCOME_FAILED = "failed"  # nothing usable came of it
_OUTCOMES = (OUTCOME_CHANGED, OUTCOME_UNCHANGED, OUTCOME_FAILED)
_TIMES = ("attempted_at", "finished_at", "succeeded_at", "changed_at")
WEEK_RETRIES_KEY = "week_retries"

# Why a week failed, which decides whether asking again could help.
FAILURE_TRANSIENT = "transient"  # Radarr or the network was down, which says nothing of Mojo
FAILURE_UNAVAILABLE = "unavailable"  # Mojo answered and has no chart for that week
FAILURE_MALFORMED = "malformed"  # Mojo has one, and it is not whole
# How many automatic attempts each gets. A passing outage earns many: it ends. A week Mojo has
# no data for gets a couple more looks in case it was posted late, and then stops, because an
# endless polite retry is still an endless retry. A card's Re-run always remains.
MAX_ATTEMPTS = {FAILURE_TRANSIENT: 8, FAILURE_UNAVAILABLE: 3, FAILURE_MALFORMED: 3}
# Doubling from here. A retry also needs a healthy check to run after, so these are floors.
FIRST_RETRY_AFTER = timedelta(hours=6)
LONGEST_RETRY_AFTER = timedelta(days=7)
# As httpx words them ("Client error '404 Not Found' for url ..."), quoted so a digit run in the
# URL cannot match. That is Mojo saying the page does not exist, not a passing fault.
_NOT_FOUND_MARKERS = ("'404 ", "'410 ")


@dataclass(frozen=True)
class ScheduledCheck:
    """The latest of each. Separate, because "it ran" and "it found something" differ."""

    attempted_at: datetime | None = None
    finished_at: datetime | None = None
    succeeded_at: datetime | None = None
    changed_at: datetime | None = None
    # None while an attempt is under way, or if the last one never got to finish.
    outcome: str | None = None


@dataclass(frozen=True)
class WeekRetry:
    """What automatic retries of one failed week have spent, surviving restarts."""

    attempts: int
    next_retry_at: datetime


def failure_class_of(report: Report) -> str:
    """Why a stored failed run failed, read from what it recorded.

    From the status and the stored sentence, because that is all a report written before
    this existed has, and those are the ones an outage already stranded.
    """
    if report.status != RunStatus.SCRAPE_FAILED:
        return FAILURE_TRANSIENT
    error = report.error or ""
    if error.startswith(FETCH_FAILED_PREFIX):
        not_found = any(marker in error for marker in _NOT_FOUND_MARKERS)
        return FAILURE_UNAVAILABLE if not_found else FAILURE_TRANSIENT
    return FAILURE_UNAVAILABLE if error.startswith(NO_DATA_PREFIX) else FAILURE_MALFORMED


def retry_is_due(retry: WeekRetry | None, failure_class: str, now: datetime) -> bool:
    if retry is None:
        return True
    return retry.attempts < MAX_ATTEMPTS[failure_class] and now >= retry.next_retry_at


def gave_up(retry: WeekRetry | None, failure_class: str) -> bool:
    return retry is not None and retry.attempts >= MAX_ATTEMPTS[failure_class]


def _next_retry_after(attempts: int) -> timedelta:
    # Capped before doubling, so the exponent cannot grow into an overflow.
    return min(FIRST_RETRY_AFTER * 2 ** min(attempts - 1, 16), LONGEST_RETRY_AFTER)


class IngestionStateStore:
    def __init__(self, config_dir: Path) -> None:
        self._path = config_dir / INGESTION_FILENAME

    def read(self) -> ScheduledCheck:
        """The stored record, strictly: raises on anything it cannot make sense of."""
        if not self._path.exists():
            return ScheduledCheck()
        document = filestore.read_yaml(self._path, expected_version=INGESTION_SCHEMA_VERSION)
        outcome = document.get("outcome")
        if outcome is not None and outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome!r}")
        times = {name: _time_from(document.get(name), name) for name in _TIMES}
        self._retries_from(document)
        return ScheduledCheck(**times, outcome=outcome)

    @staticmethod
    def _retries_from(document: dict) -> dict[str, WeekRetry]:
        stored = document.get(WEEK_RETRIES_KEY) or {}
        if not isinstance(stored, dict):
            raise ValueError("week retries are not a mapping")
        retries: dict[str, WeekRetry] = {}
        for week, entry in stored.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("attempts"), int):
                raise ValueError(f"bad retry record for {week}")
            moment = _time_from(entry.get("next_retry_at"), "next_retry_at")
            if moment is None or moment.tzinfo is None:  # naive cannot compare against now
                raise ValueError(f"bad retry record for {week}")
            retries[week] = WeekRetry(attempts=entry["attempts"], next_retry_at=moment)
        return retries

    def week_retries(self) -> dict[str, WeekRetry]:
        """Every failed week's retry record. Empty if the file is damaged: attempts start
        over, and the per-pass cap still bounds what that can cost."""
        try:
            document = filestore.read_yaml(self._path, expected_version=INGESTION_SCHEMA_VERSION)
            return self._retries_from(document)
        except (ValueError, OSError, yaml.YAMLError):
            return {}

    def record_week_failure(self, week: str, now: datetime) -> None:
        """One more automatic attempt spent on `week`, and when the next may be."""
        retries = self.week_retries()
        attempts = retries[week].attempts + 1 if week in retries else 1
        retries[week] = WeekRetry(attempts, now + _next_retry_after(attempts))
        self._save(self.load(), retries)

    def clear_week(self, week: str) -> None:
        retries = self.week_retries()
        if retries.pop(week, None) is not None:
            self._save(self.load(), retries)

    def load(self) -> ScheduledCheck:
        """The stored record, or an empty one. Scheduling must outlive a damaged file: it
        falls back to the reports, as an install does before its first check here."""
        try:
            return self.read()
        except (ValueError, OSError, yaml.YAMLError) as exc:
            print(f"ingestion state unreadable, using the reports instead: {exc}", flush=True)
            return ScheduledCheck()

    def record_attempt(self, moment: datetime) -> None:
        """Written before the work, so a run that dies part-way still counts as having gone
        to Mojo. Otherwise a crash-looping container would scrape on every boot."""
        self._save(replace(self.load(), attempted_at=moment, outcome=None), self.week_retries())

    def record_outcome(self, outcome: str, moment: datetime) -> None:
        current = replace(self.load(), finished_at=moment, outcome=outcome)
        if outcome != OUTCOME_FAILED:
            current = replace(current, succeeded_at=moment)
        if outcome == OUTCOME_CHANGED:
            current = replace(current, changed_at=moment)
        self._save(current, self.week_retries())

    def _save(self, check: ScheduledCheck, retries: dict[str, WeekRetry]) -> None:
        """A full disk is logged, not raised: bookkeeping must not stop the check itself,
        which has already recorded whatever it found."""
        document: dict[str, object] = {"outcome": check.outcome}
        for name in _TIMES:
            moment = getattr(check, name)
            document[name] = moment.isoformat() if moment else None
        document[WEEK_RETRIES_KEY] = {
            week: {"attempts": retry.attempts, "next_retry_at": retry.next_retry_at.isoformat()}
            for week, retry in retries.items()
        }
        try:
            filestore.write_yaml(self._path, document, schema_version=INGESTION_SCHEMA_VERSION)
        except OSError as exc:
            print(f"could not record the scheduled check: {exc}", flush=True)


def _time_from(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a timestamp")
    return datetime.fromisoformat(value)
