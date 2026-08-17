"""M5: the release-id cache — a cache, never a record.

Losing it costs a re-fetch. Refusing to start, or answering with something stale and
wrong, would cost the run — so every failure here reads as "not known yet".
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import filestore
from app.services.release_ids import (
    IDS_KEY,
    RELEASE_IDS_FILENAME,
    RELEASE_IDS_SCHEMA_VERSION,
    ReleaseIdCache,
)

RELEASE_PATH = "/release/rl1/"
IMDB_ID = "tt15239678"


def test_an_unresolved_path_is_simply_unknown(tmp_path: Path) -> None:
    assert ReleaseIdCache(tmp_path).get(RELEASE_PATH) is None


def test_a_resolved_id_survives_a_restart(tmp_path: Path) -> None:
    ReleaseIdCache(tmp_path).put(RELEASE_PATH, IMDB_ID)

    assert ReleaseIdCache(tmp_path).get(RELEASE_PATH) == IMDB_ID


def test_a_row_with_no_release_page_is_not_a_lookup(tmp_path: Path) -> None:
    assert ReleaseIdCache(tmp_path).get(None) is None


def test_a_second_film_does_not_displace_the_first(tmp_path: Path) -> None:
    cache = ReleaseIdCache(tmp_path)
    cache.put(RELEASE_PATH, IMDB_ID)
    cache.put("/release/rl2/", "tt9999999")

    assert cache.get(RELEASE_PATH) == IMDB_ID
    assert cache.get("/release/rl2/") == "tt9999999"


def test_writing_what_is_already_stored_does_not_touch_the_disk(tmp_path: Path) -> None:
    """A settled install re-reads this every run and should stop writing it."""
    cache = ReleaseIdCache(tmp_path)
    cache.put(RELEASE_PATH, IMDB_ID)
    path = tmp_path / RELEASE_IDS_FILENAME
    before = path.stat().st_mtime_ns

    cache.put(RELEASE_PATH, IMDB_ID)

    assert path.stat().st_mtime_ns == before


def test_the_file_is_stamped_like_every_other_store(tmp_path: Path) -> None:
    ReleaseIdCache(tmp_path).put(RELEASE_PATH, IMDB_ID)

    document = json.loads((tmp_path / RELEASE_IDS_FILENAME).read_text(encoding="utf-8"))
    assert document[filestore.SCHEMA_VERSION_KEY] == RELEASE_IDS_SCHEMA_VERSION
    assert document[IDS_KEY] == {RELEASE_PATH: IMDB_ID}


def test_an_unreadable_file_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    (tmp_path / RELEASE_IDS_FILENAME).write_text("{not json at all", encoding="utf-8")

    assert ReleaseIdCache(tmp_path).get(RELEASE_PATH) is None


def test_a_file_from_a_newer_build_reads_as_empty(tmp_path: Path) -> None:
    """A downgrade must cost a re-fetch, not a crash on every run."""
    filestore.write_json(
        tmp_path / RELEASE_IDS_FILENAME,
        {IDS_KEY: {RELEASE_PATH: IMDB_ID}},
        schema_version=RELEASE_IDS_SCHEMA_VERSION + 1,
    )

    assert ReleaseIdCache(tmp_path).get(RELEASE_PATH) is None


def test_a_writing_a_new_id_over_a_broken_file_still_works(tmp_path: Path) -> None:
    (tmp_path / RELEASE_IDS_FILENAME).write_text("[]", encoding="utf-8")
    cache = ReleaseIdCache(tmp_path)

    cache.put(RELEASE_PATH, IMDB_ID)

    assert cache.get(RELEASE_PATH) == IMDB_ID


def test_entries_that_are_not_two_strings_are_ignored(tmp_path: Path) -> None:
    """Hand-edited or half-written, the map still answers for the rows that make sense."""
    filestore.write_json(
        tmp_path / RELEASE_IDS_FILENAME,
        {IDS_KEY: {RELEASE_PATH: IMDB_ID, "/release/rl2/": 12345, "/release/rl3/": None}},
        schema_version=RELEASE_IDS_SCHEMA_VERSION,
    )
    cache = ReleaseIdCache(tmp_path)

    assert cache.get(RELEASE_PATH) == IMDB_ID
    assert cache.get("/release/rl2/") is None


def test_a_file_that_is_not_a_map_of_ids_reads_as_empty(tmp_path: Path) -> None:
    filestore.write_json(
        tmp_path / RELEASE_IDS_FILENAME,
        {IDS_KEY: "not a map"},
        schema_version=RELEASE_IDS_SCHEMA_VERSION,
    )

    assert ReleaseIdCache(tmp_path).get(RELEASE_PATH) is None


def test_a_corrected_id_replaces_the_one_stored(tmp_path: Path) -> None:
    """The skip above is a write-skip, not a write-once: a path whose id has genuinely
    changed must end up with the new one, or the cache would outrank the source forever."""
    cache = ReleaseIdCache(tmp_path)
    cache.put(RELEASE_PATH, "tt1111111")

    cache.put(RELEASE_PATH, IMDB_ID)

    assert cache.get(RELEASE_PATH) == IMDB_ID
