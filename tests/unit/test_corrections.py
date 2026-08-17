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
