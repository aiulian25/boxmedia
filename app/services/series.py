"""What your Sonarr connections hold, cached on disk (TV step 8).

Three separate questions all read this and none of them may wait on a network round
trip: *is this series already in Sonarr*, *how much of it is missing*, and — before an
Add posts anything — *do I already have this*. A card decorating a Discover shelf must
never be the reason a page hangs on a Sonarr that is switched off.

Shaped after `MediaServerLibraryCache`, with one structural difference: Sonarr is
**multi-instance** like Radarr, so this is keyed per connection. A 1080p box and a 4K
box are different libraries, and "already in Sonarr" has to be able to say which.

A cache, not a record. Unreadable, hand-edited, or written by a newer build all read as
empty: losing it costs one refetch, and refusing to render costs the page.

## Two registers, and why they are the app's existing ones

`HOLDS_YES` and `HOLDS_PROBABLY` are imported from `mediaserver` rather than restated.
They are not Plex vocabulary — they answer "how sure are we that some external library
already has this?", which is the same question for Plex-and-films as for
Sonarr-and-series. One vocabulary means the confident green line and the amber
verify-this line keep meaning exactly what they already mean everywhere else in the app.

A TVDB id match is a fact: it is what Sonarr itself keys on. A title-and-year match is a
guess and is labelled one — two shows sharing a normalized title is the reboot trap, and
claiming a 2003 original covers a 2026 revival would cause the exact double-take this
feature exists to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from app.core import filestore
from app.services.matcher import normalize_title
from app.services.mediaserver import HOLDS_PROBABLY, HOLDS_YES
from app.services.sonarr import SonarrSeries

SERIES_CACHE_SCHEMA_VERSION = 1
SERIES_CACHE_FILENAME = "sonarr-library.json"
BY_APP_KEY = "by_app"
SERIES_KEY = "series"
FETCHED_AT_KEY = "fetched_at"
# Long enough that browsing Discover costs Sonarr nothing, short enough that adding a
# series elsewhere shows up without waiting for the morning job. The media server's
# figure, for the same reasoning — a library changes on a timescale of minutes at most.
SERIES_CACHE_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class CachedSeries:
    """One series as the cache remembers it.

    Deliberately not `SonarrSeries`: that carries per-season statistics and a path, and
    this file is read on every Discover render. What survives is what a card actually
    says — the ids it is matched on, and the two counts behind "missing 8 episodes".
    """

    sonarr_id: int
    tvdb_id: int
    title: str
    year: int | None
    monitored: bool
    ended: bool
    episode_count: int
    episode_file_count: int
    imdb_id: str | None = None
    tmdb_id: int | None = None
    title_slug: str | None = None
    # Remembered so `prune` can keep it. The poster cache deletes every file no keep-set
    # names, and that set is built from the stored RECORDS — for films the weekly
    # reports, for series this. Without it, pressing Prune would wipe the artwork off
    # every TV card and the next render would re-download the lot.
    poster_url: str | None = None

    @property
    def missing_episode_count(self) -> int:
        """Aired episodes with no file. Never negative — Sonarr counts specials in some
        totals and not others, and "-2 missing" on a card would be nonsense."""
        return max(0, self.episode_count - self.episode_file_count)

    @property
    def complete(self) -> bool:
        return self.episode_count > 0 and self.missing_episode_count == 0


@dataclass(frozen=True)
class SeriesMatch:
    """Which connection holds a series, how sure we are, and what it holds.

    Carries the record rather than only a verdict because the three callers want
    different things from one lookup: the badge wants the state, the card line wants the
    counts, and the add flow wants the Sonarr id so it can offer "Open in Sonarr"
    instead of posting a duplicate.
    """

    state: str
    app_id: str
    series: CachedSeries


@dataclass(frozen=True)
class SeriesSnapshot:
    """Every connection's library resolved once, for judging a whole page against.

    The `IgnoreSnapshot` shape for the `IgnoreSnapshot` reason: every card on a page is
    judged against the same libraries, not against whatever each Sonarr happened to hold
    at the moment that row was built.
    """

    by_tvdb: dict[int, tuple[str, CachedSeries]]
    # normalized title -> the (connection, series) entries stored under it. A list, not
    # one entry: two connections can hold the same show, and so can one connection under
    # a reboot's shared title.
    by_title: dict[str, tuple[tuple[str, CachedSeries], ...]]

    def find(
        self,
        *,
        tvdb_id: int | None = None,
        title: str = "",
        year: int | None = None,
    ) -> SeriesMatch | None:
        """Whether any connection holds this series, and which.

        Ids answer first and ALONE, across every connection, before any title is
        considered: a title guess on one Sonarr must never beat an exact id match on
        another. That ordering is the whole reason this is one lookup over all
        connections rather than a loop of per-connection lookups.
        """
        if tvdb_id is not None:
            held = self.by_tvdb.get(tvdb_id)
            if held is not None:
                return SeriesMatch(state=HOLDS_YES, app_id=held[0], series=held[1])
        if not title:
            return None
        candidates = self.by_title.get(normalize_title(title))
        if not candidates:
            return None
        for app_id, series in candidates:
            # A stored series with no year of its own cannot contradict the asked year:
            # absence of evidence is not a conflicting year.
            if year is None or series.year is None or series.year == year:
                return SeriesMatch(state=HOLDS_PROBABLY, app_id=app_id, series=series)
        return None

    def holds(self, tvdb_id: int | None, title: str, year: int | None) -> str | None:
        """Just the verdict, for callers that only need the badge."""
        match = self.find(tvdb_id=tvdb_id, title=title, year=year)
        return match.state if match else None


def _cached_from(series: SonarrSeries) -> CachedSeries:
    """One live `SonarrSeries` -> the trimmed record this cache stores."""
    return CachedSeries(
        sonarr_id=series.sonarr_id,
        tvdb_id=series.tvdb_id,
        title=series.title,
        year=series.year,
        monitored=series.monitored,
        ended=series.ended,
        episode_count=series.episode_count,
        episode_file_count=series.episode_file_count,
        imdb_id=series.imdb_id,
        tmdb_id=series.tmdb_id,
        title_slug=series.title_slug,
        poster_url=series.poster_url,
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _series_from_document(entry: object) -> CachedSeries | None:
    """One stored row back into a record, or None when it is not one.

    Tolerant on purpose: this is a cache. A row a newer build wrote with a field this
    one does not know simply loses that field rather than poisoning the whole file.
    """
    if not isinstance(entry, dict):
        return None
    title = entry.get("title")
    if not isinstance(title, str) or not title:
        return None
    return CachedSeries(
        sonarr_id=_int_or_none(entry.get("sonarr_id")) or 0,
        tvdb_id=_int_or_none(entry.get("tvdb_id")) or 0,
        title=title,
        year=_int_or_none(entry.get("year")),
        monitored=bool(entry.get("monitored", False)),
        ended=bool(entry.get("ended", False)),
        episode_count=_int_or_none(entry.get("episode_count")) or 0,
        episode_file_count=_int_or_none(entry.get("episode_file_count")) or 0,
        imdb_id=entry.get("imdb_id") if isinstance(entry.get("imdb_id"), str) else None,
        tmdb_id=_int_or_none(entry.get("tmdb_id")),
        title_slug=(
            entry.get("title_slug") if isinstance(entry.get("title_slug"), str) else None
        ),
        poster_url=(
            entry.get("poster_url") if isinstance(entry.get("poster_url"), str) else None
        ),
    )


def snapshot_from(libraries: dict[str, tuple[CachedSeries, ...]]) -> SeriesSnapshot:
    """Build the lookup from each connection's series list.

    Connections are walked in the order given — the store's order, which is the order
    the Settings page lists them — so when two hold the same show the answer is stable
    across renders rather than depending on dict iteration luck.
    """
    by_tvdb: dict[int, tuple[str, CachedSeries]] = {}
    by_title: dict[str, list[tuple[str, CachedSeries]]] = {}
    for app_id, series_list in libraries.items():
        for series in series_list:
            # 0 is "Sonarr gave us no tvdb id", not a real id — indexing it would make
            # every such series match every other one.
            if series.tvdb_id and series.tvdb_id not in by_tvdb:
                by_tvdb[series.tvdb_id] = (app_id, series)
            if series.title:
                by_title.setdefault(normalize_title(series.title), []).append(
                    (app_id, series)
                )
    return SeriesSnapshot(
        by_tvdb=by_tvdb,
        by_title={title: tuple(entries) for title, entries in by_title.items()},
    )


class SeriesLibraryCache:
    """The last fetched Sonarr libraries on disk, per connection."""

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / SERIES_CACHE_FILENAME

    def _load_document(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            document = filestore.read_json(
                self._path, expected_version=SERIES_CACHE_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            # Including a schema stamp from a newer build. Losing a cache costs one
            # refetch; refusing to render costs the page.
            return {}
        by_app = document.get(BY_APP_KEY)
        return by_app if isinstance(by_app, dict) else {}

    def _write(self, by_app: dict) -> None:
        filestore.write_json(
            self._path, {BY_APP_KEY: by_app}, schema_version=SERIES_CACHE_SCHEMA_VERSION
        )

    def save(self, app_id: str, series: tuple[SonarrSeries, ...]) -> None:
        """Replace one connection's library, leaving every other connection's alone."""
        by_app = self._load_document()
        by_app[app_id] = {
            FETCHED_AT_KEY: time.time(),
            SERIES_KEY: [
                {
                    "sonarr_id": item.sonarr_id,
                    "tvdb_id": item.tvdb_id,
                    "title": item.title,
                    "year": item.year,
                    "monitored": item.monitored,
                    "ended": item.ended,
                    "episode_count": item.episode_count,
                    "episode_file_count": item.episode_file_count,
                    "imdb_id": item.imdb_id,
                    "tmdb_id": item.tmdb_id,
                    "title_slug": item.title_slug,
                    "poster_url": item.poster_url,
                }
                for item in series
            ],
        }
        self._write(by_app)

    def load(self, app_id: str) -> tuple[tuple[CachedSeries, ...], float] | None:
        """One connection's cached library and when it was fetched, or None.

        Age is the CALLER's decision, exactly as it is for the media-server cache: a
        render wants a fresh one but will take stale over nothing when Sonarr is down,
        and a Refresh button wants none at all.
        """
        stored = self._load_document().get(app_id)
        if not isinstance(stored, dict):
            return None
        rows = stored.get(SERIES_KEY)
        if not isinstance(rows, list):
            return None
        series = tuple(
            item for item in (_series_from_document(row) for row in rows) if item
        )
        fetched_at = stored.get(FETCHED_AT_KEY)
        anchor = float(fetched_at) if isinstance(fetched_at, int | float) else 0.0
        return series, anchor

    def load_all(self) -> dict[str, tuple[CachedSeries, ...]]:
        """Every connection's library, in one read — `load` per connection re-reads the
        file, and a page judges its cards against all of them at once."""
        libraries = {}
        for app_id in self._load_document():
            loaded = self.load(app_id)
            if loaded is not None:
                libraries[app_id] = loaded[0]
        return libraries

    def snapshot(self) -> SeriesSnapshot:
        """Everything held, ready to judge a page against. Empty when nothing is cached,
        which renders as "not in Sonarr" — the same answer the page gave before there
        was a cache at all."""
        return snapshot_from(self.load_all())

    def is_stale(self, app_id: str, ttl_seconds: float = SERIES_CACHE_TTL_SECONDS) -> bool:
        """Whether this connection is worth re-asking. Absent counts as stale."""
        loaded = self.load(app_id)
        if loaded is None:
            return True
        return (time.time() - loaded[1]) > ttl_seconds

    def poster_urls(self) -> set[str]:
        """Every poster the cached libraries reference, for the prune keep-set.

        Raw URLs — the caller sizes them, exactly as the reports' keep-set is sized,
        because the cache keys on the sized form and a keep-set built from the other
        form would mark every poster an orphan.
        """
        return {
            series.poster_url
            for library in self.load_all().values()
            for series in library
            if series.poster_url
        }

    def forget(self, app_id: str) -> None:
        """Drop a removed connection's library so it cannot keep decorating cards, and
        so the file does not grow forever."""
        by_app = self._load_document()
        if by_app.pop(app_id, None) is not None:
            self._write(by_app)
