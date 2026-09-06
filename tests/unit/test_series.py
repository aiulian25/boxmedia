"""TV step 8 unit test: what your Sonarr connections hold, cached and matched.

Three things carry the weight here. That a TVDB match and a title match are told apart
and labelled honestly; that an id match on ONE connection beats a title guess on
another; and that a cache which cannot be read degrades to "nothing" rather than to an
exception, because losing it costs one refetch and refusing to render costs the page.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.services.mediaserver import HOLDS_PROBABLY, HOLDS_YES
from app.services.series import (
    SERIES_CACHE_FILENAME,
    SERIES_CACHE_TTL_SECONDS,
    CachedSeries,
    SeriesLibraryCache,
    snapshot_from,
)
from app.services.sonarr import SonarrSeries

LOCAL = "app-local"
REMOTE = "app-remote"


def _sonarr_series(
    *,
    sonarr_id: int = 14,
    tvdb_id: int = 121361,
    title: str = "The Hollow Coast",
    year: int | None = 2023,
    episode_count: int = 34,
    episode_file_count: int = 26,
) -> SonarrSeries:
    return SonarrSeries(
        sonarr_id=sonarr_id,
        tvdb_id=tvdb_id,
        title=title,
        year=year,
        monitored=True,
        ended=False,
        episode_count=episode_count,
        episode_file_count=episode_file_count,
        imdb_id="tt0944947",
        tmdb_id=1396,
        title_slug="the-hollow-coast",
    )


def _cached(**changes: object) -> CachedSeries:
    base = {
        "sonarr_id": 14,
        "tvdb_id": 121361,
        "title": "The Hollow Coast",
        "year": 2023,
        "monitored": True,
        "ended": False,
        "episode_count": 34,
        "episode_file_count": 26,
    }
    return CachedSeries(**{**base, **changes})


# --- round trip ---


def test_a_library_round_trips(tmp_path: Path) -> None:
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))

    loaded = cache.load(LOCAL)
    assert loaded is not None
    series, fetched_at = loaded

    assert len(series) == 1
    assert series[0].tvdb_id == 121361
    assert series[0].title == "The Hollow Coast"
    assert series[0].sonarr_id == 14
    assert series[0].missing_episode_count == 8
    assert series[0].complete is False
    # Stamped so the caller can decide about age; the cache never decides for it.
    assert fetched_at > 0


def test_the_counts_behind_a_card_line_survive(tmp_path: Path) -> None:
    """"Missing 8 episodes" and "Complete" both come from the cache, so neither costs a
    live round trip."""
    cache = SeriesLibraryCache(tmp_path)
    cache.save(
        LOCAL,
        (
            _sonarr_series(episode_count=16, episode_file_count=16),
            _sonarr_series(sonarr_id=15, tvdb_id=99, title="Sodium Lights",
                           episode_count=20, episode_file_count=12),
        ),
    )
    complete, partial = cache.load(LOCAL)[0]

    assert complete.complete is True
    assert complete.missing_episode_count == 0
    assert partial.missing_episode_count == 8


def test_missing_count_is_never_negative(tmp_path: Path) -> None:
    """Sonarr counts specials in some totals and not others; "-2 missing" on a card
    would be nonsense."""
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(episode_count=10, episode_file_count=12),))
    assert cache.load(LOCAL)[0][0].missing_episode_count == 0


def test_saving_one_connection_leaves_the_others_alone(tmp_path: Path) -> None:
    """A 1080p box and a 4K box are different libraries. Refreshing one must not empty
    the other."""
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))
    cache.save(REMOTE, (_sonarr_series(sonarr_id=2, tvdb_id=99, title="Verdigris"),))

    cache.save(LOCAL, (_sonarr_series(title="Renamed"),))

    assert cache.load(LOCAL)[0][0].title == "Renamed"
    assert cache.load(REMOTE)[0][0].title == "Verdigris"


def test_forget_drops_only_that_connection(tmp_path: Path) -> None:
    """A library nobody is connected to must not keep decorating cards."""
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))
    cache.save(REMOTE, (_sonarr_series(sonarr_id=2, tvdb_id=99, title="Verdigris"),))

    cache.forget(LOCAL)

    assert cache.load(LOCAL) is None
    assert cache.load(REMOTE) is not None


def test_load_all_reads_the_file_once_for_every_connection(tmp_path: Path) -> None:
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))
    cache.save(REMOTE, (_sonarr_series(sonarr_id=2, tvdb_id=99, title="Verdigris"),))

    libraries = cache.load_all()
    assert set(libraries) == {LOCAL, REMOTE}
    assert libraries[REMOTE][0].title == "Verdigris"


# --- matching: a fact and a guess, told apart ---


def test_a_tvdb_match_is_a_fact(tmp_path: Path) -> None:
    """It is what Sonarr itself keys on."""
    snapshot = snapshot_from({LOCAL: (_cached(),)})
    match = snapshot.find(tvdb_id=121361, title="Anything At All", year=1999)

    assert match is not None
    assert match.state == HOLDS_YES
    assert match.app_id == LOCAL
    assert match.series.sonarr_id == 14


def test_a_title_and_year_match_is_labelled_a_guess(tmp_path: Path) -> None:
    """Two shows sharing a normalized title is the reboot trap. The amber register is
    the same one the movie flow already uses for exactly this."""
    snapshot = snapshot_from({LOCAL: (_cached(),)})
    match = snapshot.find(tvdb_id=None, title="the hollow coast", year=2023)

    assert match is not None
    assert match.state == HOLDS_PROBABLY


def test_a_conflicting_year_is_not_a_match(tmp_path: Path) -> None:
    """Claiming a 2003 original covers a 2026 revival would cause the exact double-take
    this feature exists to prevent."""
    snapshot = snapshot_from({LOCAL: (_cached(year=2003),)})
    assert snapshot.find(tvdb_id=None, title="The Hollow Coast", year=2026) is None


def test_a_stored_series_with_no_year_cannot_contradict_one(tmp_path: Path) -> None:
    """Absence of evidence is not a conflicting year."""
    snapshot = snapshot_from({LOCAL: (_cached(year=None),)})
    match = snapshot.find(tvdb_id=None, title="The Hollow Coast", year=2026)
    assert match is not None and match.state == HOLDS_PROBABLY


def test_an_id_match_anywhere_beats_a_title_guess_elsewhere(tmp_path: Path) -> None:
    """The whole reason this is ONE lookup across every connection rather than a loop of
    per-connection lookups. A guess on the box listed first must not win over a fact on
    the one listed second."""
    snapshot = snapshot_from({
        LOCAL: (_cached(tvdb_id=0, title="The Hollow Coast", year=2023),),
        REMOTE: (_cached(sonarr_id=77, tvdb_id=121361, title="Totally Different"),),
    })

    match = snapshot.find(tvdb_id=121361, title="The Hollow Coast", year=2023)

    assert match is not None
    assert match.state == HOLDS_YES
    assert match.app_id == REMOTE


def test_a_series_with_no_tvdb_id_is_not_indexed_as_id_zero(tmp_path: Path) -> None:
    """0 means "Sonarr gave us no id", not a real one — indexing it would make every
    such series match every other one."""
    snapshot = snapshot_from({
        LOCAL: (_cached(tvdb_id=0, title="One"), _cached(tvdb_id=0, title="Two")),
    })
    assert snapshot.find(tvdb_id=0, title="One", year=2023).state == HOLDS_PROBABLY
    assert snapshot.by_tvdb == {}


def test_nothing_held_answers_none(tmp_path: Path) -> None:
    snapshot = snapshot_from({})
    assert snapshot.find(tvdb_id=1, title="Whatever", year=2020) is None
    assert snapshot.holds(None, "Whatever", None) is None


def test_holds_is_the_verdict_alone(tmp_path: Path) -> None:
    snapshot = snapshot_from({LOCAL: (_cached(),)})
    assert snapshot.holds(121361, "", None) == HOLDS_YES


def test_the_snapshot_of_a_cache_judges_every_connection_at_once(tmp_path: Path) -> None:
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))
    cache.save(REMOTE, (_sonarr_series(sonarr_id=2, tvdb_id=99, title="Verdigris"),))

    snapshot = cache.snapshot()

    assert snapshot.find(tvdb_id=121361, title="", year=None).app_id == LOCAL
    assert snapshot.find(tvdb_id=99, title="", year=None).app_id == REMOTE


# --- stale tolerance: a cache, not a record ---


def test_an_unreadable_file_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    """Losing a cache costs one refetch. Refusing to render costs the page."""
    (tmp_path / SERIES_CACHE_FILENAME).write_text("{ not json at all", encoding="utf-8")
    cache = SeriesLibraryCache(tmp_path)

    assert cache.load(LOCAL) is None
    assert cache.load_all() == {}
    assert cache.snapshot().find(tvdb_id=1, title="x", year=None) is None


def test_a_newer_schema_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    (tmp_path / SERIES_CACHE_FILENAME).write_text(
        json.dumps({"schema_version": 99, "by_app": {LOCAL: {"series": []}}}),
        encoding="utf-8",
    )
    assert SeriesLibraryCache(tmp_path).load(LOCAL) is None


def test_a_malformed_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    """One bad row must not empty a whole connection's library."""
    (tmp_path / SERIES_CACHE_FILENAME).write_text(
        json.dumps({
            "schema_version": 1,
            "by_app": {LOCAL: {"fetched_at": time.time(), "series": [
                "not-a-row",
                {"tvdb_id": 5},                       # no title to render
                {"title": "", "tvdb_id": 6},          # blank title
                {"title": "Kept", "tvdb_id": 7, "year": 2020},
            ]}},
        }),
        encoding="utf-8",
    )
    series, _ = SeriesLibraryCache(tmp_path).load(LOCAL)
    assert [item.title for item in series] == ["Kept"]


def test_a_row_from_a_newer_build_loses_only_the_field_it_added(tmp_path: Path) -> None:
    """Tolerant on purpose: this is a cache, not a record."""
    (tmp_path / SERIES_CACHE_FILENAME).write_text(
        json.dumps({
            "schema_version": 1,
            "by_app": {LOCAL: {"fetched_at": time.time(), "series": [
                {"title": "Kept", "tvdb_id": 7, "year": 2020, "future_field": "?"},
            ]}},
        }),
        encoding="utf-8",
    )
    series, _ = SeriesLibraryCache(tmp_path).load(LOCAL)
    assert series[0].title == "Kept"
    assert series[0].tvdb_id == 7


def test_staleness_is_the_callers_question_and_absent_counts_as_stale(
    tmp_path: Path,
) -> None:
    cache = SeriesLibraryCache(tmp_path)
    assert cache.is_stale(LOCAL) is True  # nothing cached at all

    cache.save(LOCAL, (_sonarr_series(),))
    assert cache.is_stale(LOCAL) is False
    # Aged past its TTL, it is worth re-asking — but `load` still hands it over, because
    # stale beats nothing when Sonarr is down.
    assert cache.is_stale(LOCAL, ttl_seconds=-1.0) is True
    assert cache.load(LOCAL) is not None
    assert SERIES_CACHE_TTL_SECONDS > 0


def test_the_cache_holds_no_secret(tmp_path: Path) -> None:
    """Titles and ids only. It rides the backup like every other cache, and a backup
    must never be a way to read an API key."""
    cache = SeriesLibraryCache(tmp_path)
    cache.save(LOCAL, (_sonarr_series(),))

    raw = (tmp_path / SERIES_CACHE_FILENAME).read_text(encoding="utf-8")
    for forbidden in ("api_key", "apikey", "token", "gcm:"):
        assert forbidden not in raw.lower()


# --- review step 7: every stale connection is saved at once, on its own thread ---

# Long enough that every thread is past the read before any write lands.
READ_WINDOW_SECONDS = 0.05
CONCURRENT_CONNECTIONS = ("app-attic", "app-lounge", "app-shed", "app-loft")


def test_saving_every_library_at_once_keeps_them_all(tmp_path: Path, monkeypatch) -> None:
    """`_refresh_series` gathers one `series_library` per stale connection, and each now
    awaits its save on a worker thread. `save` reads the whole document, replaces one
    connection and writes it back, so without a lock the last writer drops the rest."""
    cache = SeriesLibraryCache(tmp_path)
    original_load = cache._load_document

    def load_slowly() -> dict:
        stored = original_load()
        time.sleep(READ_WINDOW_SECONDS)
        return stored

    monkeypatch.setattr(cache, "_load_document", load_slowly)

    threads = [
        threading.Thread(target=cache.save, args=(app_id, (_sonarr_series(),)))
        for app_id in CONCURRENT_CONNECTIONS
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(cache.load(app_id) is not None for app_id in CONCURRENT_CONNECTIONS)
