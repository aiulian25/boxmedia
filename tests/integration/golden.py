"""One seeded library, and the grid today's dashboard renders from it (TV step 16).

Ruling 4 of the TV plan says the Movies chip on the merged Library renders exactly what
the dashboard renders now. That is only a checkable claim if the "now" is written down,
so this module holds both halves: the seed, and the grid it produced against the
pre-merge dashboard, captured as `golden_movies_grid.html`.

Deliberately a fixture read from disk rather than a second implementation to compare
against — a golden file is the only kind of regression test that can catch a change
nobody thought to assert, which for a rewrite of the page that shows a person's whole
library is the risk worth spending a file on.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import respx

from app.services.matcher import normalize_title
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from tests.conftest import AppHarness
from tests.integration.conftest import queue_records

GOLDEN_PATH = Path(__file__).parent / "golden_movies_grid.html"

RADARR_URL = "http://radarr.golden:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
RADARR_NAME = "Attic Radarr"
API = f"{RADARR_URL}/api/v3"

# The grid, and only the grid: the page around it legitimately differs (its title, its
# header, its nav, its search form's action), while a card inside it must not.
_GRID = re.compile(r'<div class="poster-grid">.*?\n</div>', re.S)


def grid_of(page: str) -> str:
    """The card grid out of a rendered page, for comparing one render against another."""
    found = _GRID.search(page)
    assert found is not None, "no poster grid in the rendered page"
    return found.group(0)


def _charted(
    rank: int,
    title: str,
    status: str,
    *,
    tmdb_id: int,
    year: int | None = 2026,
    total_gross: int | None = None,
) -> MovieResult:
    return MovieResult(
        rank=rank,
        title=title,
        normalized_title=normalize_title(title),
        gross_amount=rank * 1_000_000,
        gross_display=f"${rank}.0M",
        weeks_in_release=rank,
        status=status,
        action=MovieAction.NONE,
        tmdb_id=tmdb_id,
        year=year,
        total_gross=total_gross,
        imdb_url=f"https://www.imdb.com/title/tt{tmdb_id}/",
    )


def seed_library(harness: AppHarness) -> None:
    """One library with every card state the grid can render.

    Deterministic on purpose — fixed ids, fixed weeks, no posters to fetch — so the same
    seed produces the same bytes on any machine and on any day.
    """
    harness.client.app.state.apps.add(
        name=RADARR_NAME, url=RADARR_URL, api_key=RADARR_KEY
    )
    for week, movies in (
        (
            "2026W32",
            [
                _charted(1, "Neon Rain", MovieStatus.IN_LIBRARY, tmdb_id=5001,
                         total_gross=181_500_000),
                _charted(2, "Paper Comet", MovieStatus.WANTED, tmdb_id=5002),
            ],
        ),
        (
            "2026W31",
            [
                _charted(1, "Neon Rain", MovieStatus.WANTED, tmdb_id=5001),
                _charted(3, "The Salt Line", MovieStatus.IN_LIBRARY, tmdb_id=5003),
            ],
        ),
    ):
        harness.client.app.state.reports.save(Report(
            id=f"report-{week}-100000-abcd",
            week=week,
            run_at=f"2026-08-{12 if week == '2026W32' else 5}T10:00:00+00:00",
            trigger=RunTrigger.SCHEDULED,
            status=RunStatus.OK,
            totals=ReportTotals(movies=2, matched=1),
            movies=movies,
        ))


def mock_radarr() -> None:
    """What that Radarr answers: two files on disk, one still downloading, and a title
    the reports never charted."""
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"id": 11, "tmdbId": 5001, "title": "Neon Rain", "year": 2026, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}}},
        {"id": 12, "tmdbId": 5002, "title": "Paper Comet", "year": 2026, "hasFile": False},
        {"id": 13, "tmdbId": 5003, "title": "The Salt Line", "year": 2025, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "WEBDL-2160p"}}}},
        {"id": 14, "tmdbId": 5004, "title": "Harbour Lights", "year": 2024,
         "hasFile": False, "imdbId": "tt5004"},
    ]))
    queue_records([{"movieId": 12, "size": 1000, "sizeleft": 620}])
