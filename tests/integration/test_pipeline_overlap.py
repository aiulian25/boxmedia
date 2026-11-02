"""Runs that overlap: older work never replaces newer, and a human's choice survives.

The audit's reproduction was a newer run saving gross 200 and a delayed older one overwriting
it with 100. Atomic files cannot prevent that: each write was perfectly whole.

Await order is controlled with events, not sleeps. A run holds inside its Radarr lookup, which
is where a real one spends its time, for as long as a test wants it to.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core import crypto
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services.apps import AppsStore
from app.services.backfill import BackfillRunner
from app.services.boxoffice import BoxOfficeEntry, WeekNotPublishedError
from app.services.corrections import Correction, CorrectionStore
from app.services.ignore import IgnoreStore
from app.services.pipeline import Pipeline, PipelineBusyError
from app.services.radarr import RadarrLookupResult, RadarrMovie
from app.services.reports import MATCHED_BY_GUESS, ReportsStore, RunTrigger
from app.services.scheduler import BoxMediaScheduler
from app.web.reports import RUN_BUSY_MESSAGE, RUN_BUSY_STATUS
from tests.conftest import AppHarness

WEEK = "2026W30"
TITLE = "Neon Rain"
NORMALIZED = "neon rain"
GUESSED_TMDB = 111
CORRECTED_TMDB = 222
OLDER_GROSS = 100
NEWER_GROSS = 200
REGION_AT_START = "GB"
REGION_AFTER_EDIT = "DE"
# Long enough for a second run to reach the point it would overwrite at, if nothing stopped it.
OVERLAP_WINDOW_SECONDS = 0.05
CORRECTION = Correction(tmdb_id=CORRECTED_TMDB, title="Neon Rain (2026)", year=2026)


class _HeldRadarr:
    """Holds the first lookup until told, so a test decides what happens during a run."""

    def __init__(self) -> None:
        self.in_lookup = asyncio.Event()
        self.release = asyncio.Event()
        self.holds_left = 1

    async def list_movies(self) -> list[RadarrMovie]:
        return []

    async def lookup(self, term: str) -> list[RadarrLookupResult]:
        if self.holds_left:
            self.holds_left -= 1
            self.in_lookup.set()
            await self.release.wait()
        return [
            RadarrLookupResult(
                tmdb_id=GUESSED_TMDB, title="Something Else", year=2001, overview="x",
                poster_url=None, genres=(), imdb_id=None, rating=None,
            )
        ]


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        key_file = tmp_path / "k.key"
        crypto._main(["genkey", str(key_file)])
        self.settings = Settings(
            _env_file=None, session_secret="s" * 40, encryption_key_file=key_file,
            data_dir=tmp_path / "data",
        )
        self.settings.ensure_data_dirs()
        audit = AuditLog(self.settings.logs_dir / "audit.jsonl")
        self.radarr = _HeldRadarr()
        self.reports = ReportsStore(self.settings.history_dir)
        self.corrections = CorrectionStore(self.settings.config_dir)
        self.region = REGION_AT_START
        self.grosses = [OLDER_GROSS, NEWER_GROSS]
        self.fetches = 0
        self.pipeline = Pipeline(
            apps=AppsStore(self.settings.config_dir, key=crypto.generate_key(), audit=audit),
            reports_store=self.reports,
            settings=self.settings,
            audit=audit,
            ignore_store=IgnoreStore(self.settings.config_dir, audit=audit),
            fetch_chart=self._fetch,
            make_radarr=lambda _: self.radarr,
            select_app_id=lambda: "app-1",
            region=lambda: self.region,
            corrections=self.corrections,
        )

    async def _fetch(self, week: str | None) -> tuple[str, list[BoxOfficeEntry]]:
        gross = self.grosses[min(self.fetches, len(self.grosses) - 1)]
        self.fetches += 1
        return WEEK, [
            BoxOfficeEntry(rank=1, title=TITLE, gross_amount=gross, weeks_in_release=1)
        ]

    def run(self, **kwargs: object) -> asyncio.Task:
        return asyncio.create_task(
            self.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK, **kwargs)
        )


@pytest.fixture
def fixture(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


async def test_an_older_run_cannot_overwrite_a_newer_one(fixture: _Fixture) -> None:
    """The first run read gross 100 and stalled. Whatever the second one does, the week must
    end on the figure read last."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    second = fixture.run()

    await asyncio.wait({second}, timeout=OVERLAP_WINDOW_SECONDS)
    fixture.radarr.release.set()
    await asyncio.gather(first, second)

    assert fixture.reports.latest_for_week(WEEK).movies[0].gross_amount == NEWER_GROSS
    assert len(fixture.reports.list_reports()) == 1


async def test_a_run_does_not_even_fetch_until_its_turn(fixture: _Fixture) -> None:
    """Otherwise what it holds by the time it commits is already stale."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    second = fixture.run()

    await asyncio.wait({second}, timeout=OVERLAP_WINDOW_SECONDS)

    assert fixture.fetches == 1
    fixture.radarr.release.set()
    await asyncio.gather(first, second)
    assert fixture.fetches == 2


async def test_a_correction_made_during_a_run_survives_its_commit(fixture: _Fixture) -> None:
    """The route writes both halves while the run is matching. The run read the list before
    that, so its row is a guess, and committing it would quietly undo the admin's choice."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()

    fixture.corrections.save(NORMALIZED, CORRECTION)
    fixture.reports.apply_correction(NORMALIZED, CORRECTION)

    fixture.radarr.release.set()
    await first

    row = fixture.reports.latest_for_week(WEEK).movies[0]
    assert row.tmdb_id == CORRECTED_TMDB
    assert row.title == CORRECTION.title
    assert row.detail is None
    assert row.detail != MATCHED_BY_GUESS
    assert row.normalized_title == NORMALIZED, "the chart row's identity must not move"


async def test_a_correction_in_flight_leaves_the_next_check_unchanged(fixture: _Fixture) -> None:
    """Applied through the same fingerprint-preserving path, so the next automatic check sees
    the chart it stored rather than a difference to write a fresh guess over."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    fixture.corrections.save(NORMALIZED, CORRECTION)
    fixture.radarr.release.set()
    stored = await first
    fixture.grosses = [OLDER_GROSS]

    again = await fixture.run()

    assert again.run_at == stored.run_at
    assert fixture.reports.latest_for_week(WEEK).movies[0].tmdb_id == CORRECTED_TMDB


async def test_a_region_edited_during_a_run_applies_to_the_next_one(fixture: _Fixture) -> None:
    """A chart fetched from one region must not be stored as another's."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()

    fixture.region = REGION_AFTER_EDIT
    fixture.radarr.release.set()
    await first

    assert fixture.reports.latest_for_week(WEEK).region == REGION_AT_START

    second = await fixture.run()
    assert second.region == REGION_AFTER_EDIT


async def test_cancelling_a_run_gives_its_turn_back(fixture: _Fixture) -> None:
    """A backfill is cancelled at shutdown and by a restore, mid-lookup."""
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()

    first.cancel()
    await asyncio.wait({first})

    assert first.cancelled()
    await asyncio.wait_for(fixture.run(), timeout=1.0)
    assert fixture.reports.latest_for_week(WEEK) is not None


async def test_cancelling_a_run_still_waiting_does_not_take_the_turn(fixture: _Fixture) -> None:
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    waiting = fixture.run()
    await asyncio.wait({waiting}, timeout=OVERLAP_WINDOW_SECONDS)

    waiting.cancel()
    await asyncio.wait({waiting})
    fixture.radarr.release.set()
    await first

    await asyncio.wait_for(fixture.run(), timeout=1.0)


async def test_a_run_that_raises_gives_its_turn_back(fixture: _Fixture) -> None:
    async def not_published(week: str | None) -> tuple[str, list[BoxOfficeEntry]]:
        raise WeekNotPublishedError("not yet")

    real_fetch = fixture.pipeline._fetch_chart
    fixture.pipeline._fetch_chart = not_published
    with pytest.raises(WeekNotPublishedError):
        await fixture.run()

    fixture.pipeline._fetch_chart = real_fetch
    fixture.radarr.holds_left = 0
    await asyncio.wait_for(fixture.run(), timeout=1.0)


async def test_a_manual_run_gives_up_rather_than_queueing(fixture: _Fixture) -> None:
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()

    with pytest.raises(PipelineBusyError):
        await fixture.run(admission_timeout=OVERLAP_WINDOW_SECONDS)

    assert fixture.fetches == 1, "nothing was started"
    fixture.radarr.release.set()
    await first


async def test_a_manual_run_inside_its_patience_just_waits(fixture: _Fixture) -> None:
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    second = fixture.run(admission_timeout=5.0)

    await asyncio.wait({second}, timeout=OVERLAP_WINDOW_SECONDS)
    fixture.radarr.release.set()
    await asyncio.gather(first, second)

    assert fixture.reports.latest_for_week(WEEK).movies[0].gross_amount == NEWER_GROSS


async def test_scheduled_and_backfill_work_wait_their_turn_rather_than_lose_a_week(
    fixture: _Fixture,
) -> None:
    """Unattended work has nobody to retry it, and a skipped week is a hole in the record."""
    fixture.grosses = [OLDER_GROSS, NEWER_GROSS, NEWER_GROSS + 1]
    first = fixture.run()
    await fixture.radarr.in_lookup.wait()
    scheduler = BoxMediaScheduler(fixture.pipeline, interval_hours=24)
    scheduled = asyncio.create_task(scheduler._run_scheduled())
    backfill = BackfillRunner(fixture.pipeline)
    backfilled = asyncio.create_task(backfill._fetch(WEEK))

    await asyncio.wait({scheduled, backfilled}, timeout=OVERLAP_WINDOW_SECONDS)
    assert fixture.fetches == 1

    fixture.radarr.release.set()
    await asyncio.gather(first, scheduled, backfilled)

    assert fixture.fetches == 3
    assert fixture.reports.latest_for_week(WEEK).movies[0].gross_amount == NEWER_GROSS + 1


def test_a_busy_manual_run_says_so_and_changes_nothing(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.activate()

    async def busy(**_: object) -> None:
        raise PipelineBusyError("still running")

    monkeypatch.setattr(harness.client.app.state.pipeline, "run", busy)
    before = harness.client.app.state.reports.list_reports()

    response = harness.client.post("/run", data={"week": ""}, follow_redirects=False)

    assert response.status_code == 303
    assert RUN_BUSY_STATUS in response.headers["location"]
    assert harness.client.app.state.reports.list_reports() == before
    assert RUN_BUSY_MESSAGE in harness.client.get(response.headers["location"]).text
