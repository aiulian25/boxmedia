"""Upcoming films from TMDB: opt-in, fetched only when asked, and kept apart from the charts.

A chart says what people paid to see. This says what is scheduled, which is a different and
weaker claim, so nothing here carries money and every date is treated as provisional.

The page reads the cache and only the cache. A Refresh asks for two pages at most, and there
is no job behind it, so an install that never turns this on never asks TMDB for it.

TMDB's licence allows caching for at most six months, which `load` enforces.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.core import filestore
from app.core.values import int_or_none
from app.services.boxoffice import DOMESTIC_REGION, REGIONS

UPCOMING_CACHE_FILENAME = "upcoming.json"
UPCOMING_CACHE_SCHEMA_VERSION = 1
MAX_CACHE_AGE_SECONDS = 180 * 24 * 60 * 60

DEFAULT_TERRITORY = "US"
# The Box Office regions, because those are the markets this app already speaks about. TMDB
# wants a country, and Domestic is two of them, so that one becomes its larger half.
TERRITORIES: dict[str, str] = {
    DEFAULT_TERRITORY: "United States",
    **{code: name for code, name in REGIONS.items() if code != DOMESTIC_REGION},
}
WINDOW_CHOICES_DAYS = (30, 60, 90, 180)
DEFAULT_WINDOW_DAYS = 90

_FETCHED_AT_KEY = "fetched_at"
_TERRITORY_KEY = "territory"
_WINDOW_KEY = "window_days"
_FILMS_KEY = "films"


@dataclass(frozen=True)
class UpcomingFilm:
    """One scheduled film, as TMDB gave it. Library state is resolved at render."""

    tmdb_id: int
    title: str
    release_date: date
    poster_url: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "release_date": self.release_date.isoformat(),
            "poster_url": self.poster_url,
        }


@dataclass(frozen=True)
class UpcomingShelf:
    """A fetch and what it was a fetch of, so the page labels what it holds rather than what
    Settings says now."""

    films: tuple[UpcomingFilm, ...]
    fetched_at: float
    territory: str
    window_days: int


def window_from(today: date, window_days: int) -> tuple[date, date]:
    return today, today + timedelta(days=window_days)


def _film_from_document(row: object) -> UpcomingFilm | None:
    """Tolerant: a hand-edited or damaged row is dropped, and one Refresh brings it back."""
    if not isinstance(row, dict):
        return None
    tmdb_id = int_or_none(row.get("tmdb_id"))
    title = row.get("title")
    poster = row.get("poster_url")
    try:
        released = date.fromisoformat(row.get("release_date"))
    except (TypeError, ValueError):
        return None
    if tmdb_id is None or not isinstance(title, str):
        return None
    return UpcomingFilm(tmdb_id, title, released, poster if isinstance(poster, str) else None)


class UpcomingCache:
    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / UPCOMING_CACHE_FILENAME

    def save(self, films: tuple[UpcomingFilm, ...], territory: str, window_days: int) -> None:
        filestore.write_json(
            self._path,
            {
                _FETCHED_AT_KEY: time.time(),
                _TERRITORY_KEY: territory,
                _WINDOW_KEY: window_days,
                _FILMS_KEY: [film.document() for film in films],
            },
            schema_version=UPCOMING_CACHE_SCHEMA_VERSION,
        )

    def load(self) -> UpcomingShelf | None:
        """The last fetch, or None if there is none, it is unreadable, or it is too old to
        keep showing under TMDB's licence."""
        if not self._path.exists():
            return None
        try:
            document = filestore.read_json(
                self._path, expected_version=UPCOMING_CACHE_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            return None
        fetched_at = document.get(_FETCHED_AT_KEY)
        territory = document.get(_TERRITORY_KEY)
        window_days = int_or_none(document.get(_WINDOW_KEY))
        rows = document.get(_FILMS_KEY)
        complete = (
            isinstance(fetched_at, int | float)
            and territory in TERRITORIES
            and window_days is not None
            and isinstance(rows, list)
        )
        if not complete or time.time() - fetched_at > MAX_CACHE_AGE_SECONDS:
            return None
        films = tuple(film for film in map(_film_from_document, rows) if film)
        return UpcomingShelf(films, float(fetched_at), territory, window_days)

    def forget(self) -> None:
        """Turned off, or the key that fetched it is gone: nothing of TMDB's stays behind."""
        self._path.unlink(missing_ok=True)
