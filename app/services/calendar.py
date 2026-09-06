"""One week, both media (TV step 13) — the direction's payoff.

A Radarr digital release and a Sonarr episode land in the same day column, because "what
arrives this week" is not a film question or a television question. Everything here is
composition: the two clients built in steps 3 and 9 already answer, and this normalises
their two shapes into one entry a day column can render without knowing which server it
came from.

## The state ladder, and the one refinement

The plan's ladder read: has file -> downloaded; in queue -> downloading; aired and
monitored and no file -> missing; airs today; else monitored. Implemented, "airs today"
has to come BEFORE "missing" for anything still in the future, or an episode broadcasting
at 23:00 would be called missing all day. The discriminator is not the calendar date, it
is whether the air time has PASSED:

* has a file                            -> DOWNLOADED
* in the queue                          -> DOWNLOADING (with how far along)
* still to come, today                  -> TODAY
* still to come, another day            -> MONITORED
* passed, monitored, no file            -> MISSING
* passed, unmonitored                   -> MONITORED

`MISSING` is the internal name; the page says "aired — no file yet", which is what it
means. A show that finished ten minutes ago has not been lost, it just has not been
grabbed, and the amber register already carries exactly that meaning everywhere else.

## A cache, not a record

Unreadable, hand-edited, or written by a newer build all read as empty. A connection that
does not answer leaves its PREVIOUS entries in place and the answer is marked stale —
"we could not look" and "nothing is due" are different claims, and only one of them is
true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.core import filestore
from app.core.values import float_or_none, int_or_none
from app.services.radarr import RadarrRelease
from app.services.sonarr import SonarrCalendarEpisode

CALENDAR_CACHE_SCHEMA_VERSION = 1
CALENDAR_CACHE_FILENAME = "calendar.json"
ENTRIES_KEY = "entries"
FETCHED_AT_KEY = "fetched_at"
# Long enough that clicking between weeks costs nothing, short enough that a grab that
# landed while you were reading shows up on the next page you open. The morning job
# (step 15) is what keeps it warm; this is the "somebody opened the page" path.
CALENDAR_CACHE_TTL_SECONDS = 15 * 60

# What a calendar entry is about. Mirrored from `ignore.KIND_*` rather than imported —
# a calendar has no business depending on the ignore store for two strings — and pinned
# equal by a test, the deliberate mirror `backup.py` already documents.
KIND_MOVIE = "movie"
KIND_SERIES = "series"

# How far either side of this week the cache reaches. The page shows one week plus a
# fortnight's look-ahead and can step to the neighbouring weeks, so the window has to
# cover all three without a refetch per click.
WINDOW_DAYS = 14

# The ladder, named so no caller matches on a bare string it happens to know.
STATE_DOWNLOADED = "downloaded"
STATE_DOWNLOADING = "downloading"
STATE_MISSING = "missing"
STATE_TODAY = "today"
STATE_MONITORED = "monitored"


@dataclass(frozen=True)
class CalendarEntry:
    """One thing arriving, whichever server it came from.

    `sub` is the second line — an episode code and time, or which release this is. Built
    by the reader that knows the source, so the day column places it without having to
    ask what kind of thing it is holding.
    """

    kind: str
    title: str
    sub: str
    when: datetime
    state: str
    connection: str
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    progress: float | None = None

    @property
    def day(self) -> date:
        """Which column this belongs in. UTC, matching how both servers report."""
        return self.when.astimezone(UTC).date()

    def document(self) -> dict[str, object]:
        return {
            "kind": self.kind, "title": self.title, "sub": self.sub,
            "when": self.when.isoformat(), "state": self.state,
            "connection": self.connection, "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id, "progress": self.progress,
        }


def window_for(today: date, *, days: int = WINDOW_DAYS) -> tuple[datetime, datetime]:
    """The span the cache covers: this week, plus `days` either side.

    Anchored on the WEEK rather than on today, so the answer does not shift under a page
    that is showing Monday-to-Sunday — and so stepping to the previous or next week is a
    read rather than a refetch.
    """
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    start = datetime.combine(week_start - timedelta(days=days), datetime.min.time(), UTC)
    end = datetime.combine(week_end + timedelta(days=days), datetime.max.time(), UTC)
    return start, end


def _state(
    *,
    when: datetime,
    has_file: bool,
    monitored: bool,
    progress: float | None,
    now: datetime,
) -> str:
    """The ladder. See the module docstring for why "today" outranks "missing"."""
    if has_file:
        return STATE_DOWNLOADED
    if progress is not None:
        return STATE_DOWNLOADING
    if when > now:
        # Still to come. Today's column gets its own state so the page can say "airs
        # tonight" rather than the same word it uses for a fortnight away.
        airs_today = when.astimezone(UTC).date() == now.astimezone(UTC).date()
        return STATE_TODAY if airs_today else STATE_MONITORED
    if monitored:
        # Aired and not here. Not lost — not grabbed yet, which is what the page says.
        return STATE_MISSING
    return STATE_MONITORED


def entry_from_episode(
    episode: SonarrCalendarEpisode,
    *,
    connection: str,
    progress: float | None,
    now: datetime,
) -> CalendarEntry | None:
    """One Sonarr episode as a calendar entry, or None when it has no air time.

    An episode with no date cannot be placed in a day column, and inventing one would put
    it under a heading it does not belong to.
    """
    if episode.air_date_utc is None:
        return None
    code = f"S{episode.season_number:02d}E{episode.episode_number:02d}"
    at = episode.air_date_utc.astimezone(UTC)
    parts = [code, f"{at.hour:02d}:{at.minute:02d}"]
    if episode.title:
        parts.append(episode.title)
    return CalendarEntry(
        kind=KIND_SERIES,
        title=episode.series_title,
        sub=" · ".join(parts),
        when=episode.air_date_utc,
        state=_state(
            when=episode.air_date_utc, has_file=episode.has_file,
            monitored=episode.monitored, progress=progress, now=now,
        ),
        connection=connection,
        tvdb_id=episode.tvdb_id,
        progress=progress,
    )


def entry_from_release(
    release: RadarrRelease,
    *,
    connection: str,
    progress: float | None,
    now: datetime,
) -> CalendarEntry:
    """One Radarr release as a calendar entry."""
    return CalendarEntry(
        kind=KIND_MOVIE,
        title=release.title,
        sub=release.release_name,
        when=release.when,
        state=_state(
            when=release.when, has_file=release.has_file,
            monitored=release.monitored, progress=progress, now=now,
        ),
        connection=connection,
        tmdb_id=release.tmdb_id,
        progress=progress,
    )


def _entry_from_document(row: object) -> CalendarEntry | None:
    """One stored row back into an entry, or None when it is not one.

    Tolerant: this is a cache. A row a newer build wrote with a field this one does not
    know loses that field rather than emptying the week.
    """
    if not isinstance(row, dict):
        return None
    title = row.get("title")
    when = row.get("when")
    if not isinstance(title, str) or not title or not isinstance(when, str):
        return None
    try:
        parsed = datetime.fromisoformat(when)
    except ValueError:
        return None

    progress = row.get("progress")
    return CalendarEntry(
        kind=KIND_SERIES if row.get("kind") == KIND_SERIES else KIND_MOVIE,
        title=title,
        sub=row.get("sub") if isinstance(row.get("sub"), str) else "",
        when=parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC),
        state=row.get("state") if isinstance(row.get("state"), str) else STATE_MONITORED,
        connection=row.get("connection") if isinstance(row.get("connection"), str) else "",
        tmdb_id=int_or_none(row.get("tmdb_id")),
        tvdb_id=int_or_none(row.get("tvdb_id")),
        progress=float_or_none(progress),
    )


def sorted_entries(entries: list[CalendarEntry]) -> list[CalendarEntry]:
    """Chronological, then by title so a day's rows do not reshuffle between renders.

    Stability matters more than it sounds: two episodes at the same minute reordering on
    every page load reads as the page changing its mind about what is on.
    """
    return sorted(entries, key=lambda entry: (entry.when, entry.title, entry.sub))


class CalendarCache:
    """The last merged calendar on disk.

    One file rather than per connection: unlike a library, this is always read as a whole
    — a day column shows everything arriving that day, whichever server it came from.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / CALENDAR_CACHE_FILENAME

    def _document(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return filestore.read_json(
                self._path, expected_version=CALENDAR_CACHE_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            return {}

    def save(self, entries: list[CalendarEntry]) -> None:
        """Written in whatever order they were fetched — `load` is what puts them in
        order, because it has to do that for a hand-edited file anyway."""
        filestore.write_json(
            self._path,
            {
                FETCHED_AT_KEY: time.time(),
                ENTRIES_KEY: [entry.document() for entry in entries],
            },
            schema_version=CALENDAR_CACHE_SCHEMA_VERSION,
        )

    def load(self) -> list[CalendarEntry]:
        """Everything cached, in order. Empty when nothing is, which a page renders as an
        honest "nothing fetched yet" rather than as a failure."""
        stored = self._document().get(ENTRIES_KEY)
        rows = stored if isinstance(stored, list) else []
        return sorted_entries(
            [entry for entry in (_entry_from_document(row) for row in rows) if entry]
        )

    def fetched_at(self) -> float | None:
        value = self._document().get(FETCHED_AT_KEY)
        return float(value) if isinstance(value, int | float) else None

    def is_stale(self, ttl_seconds: float = CALENDAR_CACHE_TTL_SECONDS) -> bool:
        """Whether it is worth asking again. Never-fetched counts as stale."""
        fetched_at = self.fetched_at()
        return fetched_at is None or (time.time() - fetched_at) > ttl_seconds

    def forget(self) -> None:
        self._path.unlink(missing_ok=True)


def entries_for_day(entries: list[CalendarEntry], day: date) -> list[CalendarEntry]:
    """One column's rows. A plain filter, kept here so the page never re-derives which
    UTC day an entry belongs to — that rule lives on the entry."""
    return [entry for entry in entries if entry.day == day]
