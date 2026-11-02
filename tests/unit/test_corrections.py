"""Corrections: what an admin confirmed, remembered as a fact about a chart title."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import filestore
from app.services.corrections import (
    BY_TITLE_KEY,
    CORRECTIONS_FILENAME,
    CORRECTIONS_SCHEMA_VERSION,
    Correction,
    CorrectionStore,
)

CHART_TITLE = "miroirs no 3"
CONFIRMED = Correction(
    tmdb_id=111, title="Mirrors No. 3", year=2025,
    imdb_url="https://www.imdb.com/title/tt15239678/", poster_url="http://img/ok.jpg",
)


def test_nothing_is_corrected_until_someone_corrects_it(tmp_path: Path) -> None:
    assert CorrectionStore(tmp_path).get(CHART_TITLE) is None
    assert CorrectionStore(tmp_path).all() == {}


def test_a_confirmation_survives_a_restart(tmp_path: Path) -> None:
    CorrectionStore(tmp_path).save(CHART_TITLE, CONFIRMED)

    stored = CorrectionStore(tmp_path).get(CHART_TITLE)
    assert stored == CONFIRMED


def test_correcting_the_same_title_again_replaces_the_answer(tmp_path: Path) -> None:
    """What makes a wrong correction recoverable without hand-editing a file: fix the row
    again and the new answer simply takes over."""
    store = CorrectionStore(tmp_path)
    store.save(CHART_TITLE, CONFIRMED)

    store.save(CHART_TITLE, Correction(tmdb_id=222, title="Something Else"))

    assert store.get(CHART_TITLE).tmdb_id == 222
    assert len(store.all()) == 1, "the old answer was kept alongside the new one"


def test_a_second_title_does_not_displace_the_first(tmp_path: Path) -> None:
    store = CorrectionStore(tmp_path)
    store.save(CHART_TITLE, CONFIRMED)
    store.save("skin crawl", Correction(tmdb_id=333, title="Skin Crawl"))

    assert set(store.all()) == {CHART_TITLE, "skin crawl"}


def test_the_file_is_stamped_like_every_other_store(tmp_path: Path) -> None:
    CorrectionStore(tmp_path).save(CHART_TITLE, CONFIRMED)

    document = filestore.read_yaml(
        tmp_path / CORRECTIONS_FILENAME, expected_version=CORRECTIONS_SCHEMA_VERSION
    )
    assert document[BY_TITLE_KEY][CHART_TITLE]["tmdb_id"] == 111


def test_a_correction_needs_only_a_film_to_point_at(tmp_path: Path) -> None:
    """Radarr answers plenty of lookups with no year, no IMDb id and no artwork. None of
    that is a reason to refuse a confirmation the admin has already made."""
    store = CorrectionStore(tmp_path)
    store.save(CHART_TITLE, Correction(tmdb_id=111, title="Mirrors No. 3"))

    stored = store.get(CHART_TITLE)
    assert (stored.year, stored.imdb_url, stored.poster_url) == (None, None, None)


def test_a_file_that_is_not_a_map_reads_as_no_corrections(tmp_path: Path) -> None:
    filestore.write_yaml(
        tmp_path / CORRECTIONS_FILENAME,
        {BY_TITLE_KEY: "not a map"},
        schema_version=CORRECTIONS_SCHEMA_VERSION,
    )

    assert CorrectionStore(tmp_path).all() == {}


def test_a_file_from_a_newer_build_is_not_read_as_empty(tmp_path: Path) -> None:
    """Unlike the release-id cache, this is a RECORD: an admin's decision. Silently
    ignoring it would quietly undo work they did, so it fails loudly instead."""
    filestore.write_yaml(
        tmp_path / CORRECTIONS_FILENAME,
        {BY_TITLE_KEY: {CHART_TITLE: CONFIRMED.model_dump()}},
        schema_version=CORRECTIONS_SCHEMA_VERSION + 1,
    )

    with pytest.raises(filestore.SchemaVersionError):
        CorrectionStore(tmp_path).all()


# --- a title is not an identity ---

ORIGINAL = Correction(
    tmdb_id=1001, title="Nosferatu", year=1922, release_path="/release/rl1922/", imdb_id="tt0013442"
)
REMAKE = Correction(
    tmdb_id=2024, title="Nosferatu", year=2024, release_path="/release/rl2024/", imdb_id="tt5040012"
)
NAMESAKES = "nosferatu"


def test_two_namesakes_keep_their_own_corrections(tmp_path: Path) -> None:
    """One title used to hold one answer, so correcting either overwrote the other."""
    store = CorrectionStore(tmp_path)
    store.save(NAMESAKES, ORIGINAL)
    store.save(NAMESAKES, REMAKE)

    book = store.book()

    assert book.for_row(NAMESAKES, ORIGINAL.release_path, None) == ORIGINAL
    assert book.for_row(NAMESAKES, REMAKE.release_path, None) == REMAKE


def test_a_release_correction_does_not_reach_a_namesake(tmp_path: Path) -> None:
    store = CorrectionStore(tmp_path)
    store.save(NAMESAKES, ORIGINAL)

    assert store.book().for_row(NAMESAKES, "/release/rlother/", None) is None
    assert store.book().for_row(NAMESAKES, None, None) is None


def test_a_re_release_of_the_same_film_inherits_by_its_id(tmp_path: Path) -> None:
    """A re-release has its own path. The film is the same, and the id says so."""
    store = CorrectionStore(tmp_path)
    store.save(NAMESAKES, ORIGINAL)

    found = store.book().for_row(NAMESAKES, "/release/rlre/", ORIGINAL.imdb_id)

    assert found == ORIGINAL


def test_a_title_only_correction_still_applies_where_nothing_contradicts_it(
    tmp_path: Path,
) -> None:
    """Somebody made that decision. It cannot be split after the fact, and dropping it
    would silently undo it."""
    store = CorrectionStore(tmp_path)
    legacy = Correction(
        tmdb_id=1, title="Nosferatu", imdb_url="https://www.imdb.com/title/tt0013442/"
    )
    store.save(NAMESAKES, legacy)

    book = store.book()

    assert book.for_row(NAMESAKES, None, None) == legacy
    assert book.for_row(NAMESAKES, "/release/rlany/", None) == legacy
    assert book.for_row(NAMESAKES, "/release/rlany/", "tt0013442") == legacy


def test_a_title_only_correction_yields_when_the_source_says_otherwise(tmp_path: Path) -> None:
    """A remake charting years later has the title, not the film."""
    store = CorrectionStore(tmp_path)
    store.save(
        NAMESAKES,
        Correction(
            tmdb_id=1, title="Nosferatu", imdb_url="https://www.imdb.com/title/tt0013442/"
        ),
    )

    assert store.book().for_row(NAMESAKES, "/release/rl2024/", "tt5040012") is None


def test_a_file_written_before_releases_were_recorded_still_reads(tmp_path: Path) -> None:
    from app.core import filestore

    filestore.write_yaml(
        tmp_path / CORRECTIONS_FILENAME,
        {"by_title": {NAMESAKES: {"tmdb_id": 1, "title": "Nosferatu"}}},
        schema_version=CORRECTIONS_SCHEMA_VERSION,
    )

    assert CorrectionStore(tmp_path).get(NAMESAKES).tmdb_id == 1
    assert CorrectionStore(tmp_path).book().by_release == {}


def test_saving_one_scope_keeps_the_other(tmp_path: Path) -> None:
    store = CorrectionStore(tmp_path)
    store.save(CHART_TITLE, CONFIRMED)
    store.save(NAMESAKES, ORIGINAL)

    assert store.get(CHART_TITLE) == CONFIRMED
    assert store.book().by_release[ORIGINAL.release_path] == ORIGINAL

