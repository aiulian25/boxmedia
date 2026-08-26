"""TV step 13 unit test: one week, both media.

Three things carry the weight. That the state ladder says the true thing at every point
on a frozen clock — in particular that an episode airing at 23:00 does not read as
missing all day. That a film and an episode land in the SAME day column, which is the
whole point of merging two servers into one calendar. And that a connection which cannot
be reached keeps the rows it gave last time instead of quietly emptying the week.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from app.services import ignore
from app.services.calendar import (
    CALENDAR_CACHE_FILENAME,
    CALENDAR_CACHE_SCHEMA_VERSION,
    CALENDAR_CACHE_TTL_SECONDS,
    KIND_MOVIE,
    KIND_SERIES,
    STATE_DOWNLOADED,
    STATE_DOWNLOADING,
    STATE_MISSING,
    STATE_MONITORED,
    STATE_TODAY,
    CalendarCache,
    CalendarEntry,
    entries_for_day,
    entry_from_episode,
    entry_from_release,
    window_for,
)
from app.services.radarr import RELEASE_DIGITAL, RELEASE_IN_CINEMAS, RadarrRelease
from app.services.sonarr import SonarrCalendarEpisode

# A Thursday, and a clock stopped at mid-afternoon so "later today" and "earlier today"
# both exist. Every state below is read against this one instant.
NOW = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
TONIGHT = datetime(2026, 8, 27, 23, 0, tzinfo=UTC)
THIS_MORNING = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
NEXT_WEEK = datetime(2026, 9, 3, 21, 0, tzinfo=UTC)
LAST_WEEK = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)

SONARR_CONNECTION = "Attic Sonarr"
RADARR_CONNECTION = "Attic Radarr"


def _episode(
    *,
    when: datetime | None = TONIGHT,
    has_file: bool = False,
    monitored: bool = True,
    title: str | None = "The Long Way Down",
    season_number: int = 2,
    episode_number: int = 7,
    series_id: int = 14,
) -> SonarrCalendarEpisode:
    return SonarrCalendarEpisode(
        episode_id=901,
        series_id=series_id,
        series_title="The Hollow Coast",
        tvdb_id=121361,
        season_number=season_number,
        episode_number=episode_number,
        title=title,
        air_date_utc=when,
        has_file=has_file,
        monitored=monitored,
    )


def _release(
    *,
    when: datetime = TONIGHT,
    has_file: bool = False,
    monitored: bool = True,
    release_kind: str = RELEASE_DIGITAL,
    radarr_id: int = 88,
    title: str = "Harbour Lights",
) -> RadarrRelease:
    return RadarrRelease(
        radarr_id=radarr_id,
        tmdb_id=550,
        title=title,
        year=2026,
        release_kind=release_kind,
        when=when,
        has_file=has_file,
        monitored=monitored,
    )


def _entry(episode: SonarrCalendarEpisode, progress: float | None = None) -> CalendarEntry:
    entry = entry_from_episode(
        episode, connection=SONARR_CONNECTION, progress=progress, now=NOW
    )
    assert entry is not None
    return entry


class TestTheStateLadder:
    """Every rung, on a stopped clock. The order matters more than any single rung: a
    file present outranks a queue entry, and a queue entry outranks the air time."""

    def test_an_episode_with_a_file_is_downloaded(self) -> None:
        assert _entry(_episode(has_file=True)).state == STATE_DOWNLOADED

    def test_a_file_outranks_a_queue_entry(self) -> None:
        # Radarr and Sonarr both leave an import in the queue for a moment after the file
        # lands. "Downloaded" is the truer of the two claims.
        assert _entry(_episode(has_file=True), progress=42.0).state == STATE_DOWNLOADED

    def test_an_episode_in_the_queue_is_downloading(self) -> None:
        entry = _entry(_episode(when=THIS_MORNING), progress=61.5)
        assert entry.state == STATE_DOWNLOADING
        assert entry.progress == 61.5

    def test_a_queue_entry_at_zero_still_counts_as_downloading(self) -> None:
        # 0.0 is a real answer — queued, nothing transferred yet — and `if progress:`
        # would read it as absent and call the episode missing instead.
        assert _entry(_episode(when=THIS_MORNING), progress=0.0).state == STATE_DOWNLOADING

    def test_an_episode_airing_later_today_is_not_missing(self) -> None:
        # The refinement the module documents: 23:00 tonight is not a lost episode at
        # 15:00, and calling it missing all day would be wrong for eight hours.
        assert _entry(_episode(when=TONIGHT)).state == STATE_TODAY

    def test_an_episode_that_aired_this_morning_with_no_file_is_missing(self) -> None:
        assert _entry(_episode(when=THIS_MORNING)).state == STATE_MISSING

    def test_a_future_episode_on_another_day_is_merely_monitored(self) -> None:
        assert _entry(_episode(when=NEXT_WEEK)).state == STATE_MONITORED

    def test_an_unmonitored_episode_that_aired_is_not_called_missing(self) -> None:
        # Nobody asked for it, so nothing is missing. Amber here would be a false alarm.
        assert _entry(_episode(when=LAST_WEEK, monitored=False)).state == STATE_MONITORED

    def test_a_monitored_episode_that_aired_last_week_is_missing(self) -> None:
        assert _entry(_episode(when=LAST_WEEK)).state == STATE_MISSING

    def test_the_ladder_reads_the_same_for_a_film(self) -> None:
        # One ladder, both media — a film released this morning with no file is in the
        # same amber state as an episode that aired this morning.
        entry = entry_from_release(
            _release(when=THIS_MORNING), connection=RADARR_CONNECTION,
            progress=None, now=NOW,
        )
        assert entry.state == STATE_MISSING
        assert entry_from_release(
            _release(when=TONIGHT), connection=RADARR_CONNECTION, progress=None, now=NOW
        ).state == STATE_TODAY


class TestWhatAnEntrySays:
    def test_an_episode_carries_its_code_time_and_title(self) -> None:
        sub = _entry(_episode()).sub
        assert sub.startswith("S02E07")
        assert "23:00" in sub
        assert "The Long Way Down" in sub

    def test_an_episode_with_no_title_still_reads(self) -> None:
        # Sonarr withholds titles for unaired episodes of some series. A trailing
        # separator with nothing after it would look like a rendering bug.
        sub = _entry(_episode(title=None)).sub
        assert sub == "S02E07 · 23:00"

    def test_an_episode_with_no_air_date_is_not_placed_at_all(self) -> None:
        # There is no honest column for it, and inventing one files it under a day it
        # does not belong to.
        assert entry_from_episode(
            _episode(when=None), connection=SONARR_CONNECTION, progress=None, now=NOW
        ) is None

    def test_a_film_says_which_release_this_is(self) -> None:
        entry = entry_from_release(
            _release(release_kind=RELEASE_IN_CINEMAS), connection=RADARR_CONNECTION,
            progress=None, now=NOW,
        )
        assert entry.sub == "In cinemas"
        assert entry.kind == KIND_MOVIE
        assert entry.tmdb_id == 550

    def test_the_two_kinds_are_the_ones_the_rest_of_the_app_uses(self) -> None:
        # Mirrored from the ignore store rather than imported, and pinned here so the
        # mirror cannot drift — the deliberate-mirror convention `backup.py` documents.
        assert (KIND_MOVIE, KIND_SERIES) == (ignore.KIND_MOVIE, ignore.KIND_SERIES)

    def test_a_row_from_another_timezone_still_lands_on_its_utc_day(self) -> None:
        # Nothing writes one today, but a day column that trusted a local offset would
        # file a 13:00+13:00 row under the wrong day the moment one appeared.
        far_east = CalendarEntry(
            kind=KIND_MOVIE, title="Dateline", sub="",
            when=datetime(2026, 8, 28, 11, 0, tzinfo=timezone(timedelta(hours=13))),
            state=STATE_MONITORED, connection=RADARR_CONNECTION,
        )
        assert far_east.day == date(2026, 8, 27)

    def test_a_day_is_read_in_utc(self) -> None:
        # 00:30 UTC is the 27th. A local-time reading would file it under the 26th for
        # anyone west of Greenwich, which is how a calendar loses a day.
        entry = _entry(_episode(when=datetime(2026, 8, 27, 0, 30, tzinfo=UTC)))
        assert entry.day == date(2026, 8, 27)


class TestOneWeekBothMedia:
    def test_a_film_and_an_episode_share_a_day(self) -> None:
        entries = [
            _entry(_episode(when=TONIGHT)),
            entry_from_release(
                _release(when=datetime(2026, 8, 27, 9, 0, tzinfo=UTC)),
                connection=RADARR_CONNECTION, progress=None, now=NOW,
            ),
            _entry(_episode(when=NEXT_WEEK)),
        ]
        thursday = entries_for_day(entries, date(2026, 8, 27))
        assert len(thursday) == 2
        assert {entry.kind for entry in thursday} == {KIND_MOVIE, KIND_SERIES}
        assert {entry.connection for entry in thursday} == {
            SONARR_CONNECTION, RADARR_CONNECTION
        }

    def test_a_day_with_nothing_on_it_is_empty_rather_than_an_error(self) -> None:
        assert entries_for_day([_entry(_episode())], date(2026, 8, 25)) == []


class TestTheWindow:
    def test_it_is_anchored_on_the_week_not_on_today(self) -> None:
        # Thursday and the Monday of the same week must ask for the same span, or the
        # answer shifts under a page that is showing Monday to Sunday.
        assert window_for(date(2026, 8, 27)) == window_for(date(2026, 8, 24))

    def test_it_reaches_a_fortnight_either_side_of_the_week(self) -> None:
        start, end = window_for(date(2026, 8, 27))
        assert start.date() == date(2026, 8, 10)  # Monday 24th − 14 days
        assert end.date() == date(2026, 9, 13)  # Sunday 30th + 14 days
        assert start.tzinfo is not None and end.tzinfo is not None

    def test_the_last_day_is_covered_to_its_final_moment(self) -> None:
        # Ending at midnight would drop everything airing ON the last day, which is a
        # whole day of the calendar silently missing.
        _, end = window_for(date(2026, 8, 27))
        assert end.hour == 23 and end.minute == 59

    def test_a_sunday_belongs_to_the_week_that_is_ending(self) -> None:
        # Sunday is the last day of the week here, not the first day of the next one.
        assert window_for(date(2026, 8, 30)) == window_for(date(2026, 8, 24))


class TestTheCache:
    def test_it_survives_a_round_trip(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([_entry(_episode(when=THIS_MORNING), progress=61.5)])
        loaded = cache.load()
        assert len(loaded) == 1
        assert loaded[0].title == "The Hollow Coast"
        assert loaded[0].when == THIS_MORNING
        assert loaded[0].state == STATE_DOWNLOADING
        assert loaded[0].progress == 61.5
        assert loaded[0].tvdb_id == 121361
        assert loaded[0].kind == KIND_SERIES
        assert loaded[0].connection == SONARR_CONNECTION

    def test_a_film_survives_a_round_trip_too(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([
            entry_from_release(
                _release(), connection=RADARR_CONNECTION, progress=None, now=NOW
            )
        ])
        loaded = cache.load()
        assert loaded[0].kind == KIND_MOVIE
        assert loaded[0].tmdb_id == 550
        assert loaded[0].tvdb_id is None
        assert loaded[0].progress is None

    def test_rows_come_back_in_time_order(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([
            _entry(_episode(when=NEXT_WEEK)),
            _entry(_episode(when=THIS_MORNING)),
            _entry(_episode(when=TONIGHT)),
        ])
        assert [entry.when for entry in cache.load()] == [
            THIS_MORNING, TONIGHT, NEXT_WEEK
        ]

    def test_two_rows_at_the_same_minute_do_not_reshuffle(self, tmp_path: Path) -> None:
        # Two episodes at 21:00 swapping places on every render reads as the page
        # changing its mind about what is on.
        cache = CalendarCache(tmp_path)
        cache.save([
            entry_from_release(
                _release(when=TONIGHT, title="Zephyr"), connection=RADARR_CONNECTION,
                progress=None, now=NOW,
            ),
            entry_from_release(
                _release(when=TONIGHT, title="Anvil"), connection=RADARR_CONNECTION,
                progress=None, now=NOW,
            ),
        ])
        assert [entry.title for entry in cache.load()] == ["Anvil", "Zephyr"]

    def test_nothing_cached_reads_as_nothing_cached(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        assert cache.load() == []
        assert cache.fetched_at() is None
        assert cache.is_stale() is True

    def test_a_fresh_fetch_is_not_stale(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([_entry(_episode())])
        assert cache.is_stale() is False
        assert cache.fetched_at() is not None

    def test_an_old_fetch_is_stale(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([_entry(_episode())])
        assert cache.is_stale(ttl_seconds=-1) is True

    def test_the_ttl_is_the_quarter_hour_the_page_expects(self) -> None:
        assert CALENDAR_CACHE_TTL_SECONDS == 900

    def test_a_hand_edited_file_reads_as_empty_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        # A cache is not a record: losing it costs one refetch, refusing to render costs
        # the page.
        (tmp_path / CALENDAR_CACHE_FILENAME).write_text("{ this is not json", "utf-8")
        assert CalendarCache(tmp_path).load() == []

    def test_a_file_from_a_newer_build_reads_as_empty(self, tmp_path: Path) -> None:
        (tmp_path / CALENDAR_CACHE_FILENAME).write_text(
            json.dumps({
                "schema_version": CALENDAR_CACHE_SCHEMA_VERSION + 1,
                "entries": [{"title": "Whatever", "when": NOW.isoformat()}],
            }),
            "utf-8",
        )
        assert CalendarCache(tmp_path).load() == []

    def test_one_unreadable_row_does_not_cost_the_others(self, tmp_path: Path) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([_entry(_episode())])
        path = tmp_path / CALENDAR_CACHE_FILENAME
        document = json.loads(path.read_text("utf-8"))
        document["entries"] = [
            "not a row",
            {"title": "", "when": NOW.isoformat()},
            {"title": "No date"},
            {"title": "Bad date", "when": "the third of never"},
            *document["entries"],
        ]
        path.write_text(json.dumps(document), "utf-8")
        assert [entry.title for entry in cache.load()] == ["The Hollow Coast"]

    def test_a_stored_true_is_not_read_as_an_id(self, tmp_path: Path) -> None:
        # JSON true is an int to `isinstance`, so a row carrying one would come back with
        # tmdb_id True and be looked up as film number 1.
        path = tmp_path / CALENDAR_CACHE_FILENAME
        path.write_text(
            json.dumps({
                "schema_version": CALENDAR_CACHE_SCHEMA_VERSION,
                "fetched_at": 0,
                "entries": [{
                    "title": "Bool", "when": TONIGHT.isoformat(),
                    "tmdb_id": True, "tvdb_id": True, "progress": True,
                }],
            }),
            "utf-8",
        )
        entry = CalendarCache(path.parent).load()[0]
        assert (entry.tmdb_id, entry.tvdb_id, entry.progress) == (None, None, None)

    def test_a_stored_row_without_a_zone_is_read_as_utc(self, tmp_path: Path) -> None:
        # Both servers report UTC. A naive string read as local time would move rows
        # between day columns on any machine that is not on UTC.
        path = tmp_path / CALENDAR_CACHE_FILENAME
        path.write_text(
            json.dumps({
                "schema_version": CALENDAR_CACHE_SCHEMA_VERSION,
                "fetched_at": 0,
                "entries": [{"title": "Naive", "when": "2026-08-27T23:00:00"}],
            }),
            "utf-8",
        )
        assert CalendarCache(path.parent).load()[0].when == TONIGHT

    def test_forget_removes_it_and_forgetting_twice_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        cache = CalendarCache(tmp_path)
        cache.save([_entry(_episode())])
        cache.forget()
        cache.forget()
        assert cache.load() == []
        assert not (tmp_path / CALENDAR_CACHE_FILENAME).exists()
