"""IgnoreSnapshot: same answers as the per-call lookup, one file read instead of N."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.audit import AuditLog
from app.services.ignore import IgnoreSnapshot, IgnoreStore


@pytest.fixture
def store(tmp_path: Path) -> IgnoreStore:
    return IgnoreStore(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"))


def _expected_is_ignored(store: IgnoreStore, tmdb_id: int | None, title: str) -> bool:
    """The rule, written out longhand as the specification the snapshot must satisfy.

    Was the pre-snapshot implementation; review Step 10 tightened it so the title
    fallback applies only when a side has no id to compare. Kept in this shape so the
    snapshot's set arithmetic is checked against something readable rather than itself.
    """
    for movie in store.list_ignored():
        if tmdb_id is not None and movie.tmdb_id == tmdb_id:
            return True
        unidentified = movie.tmdb_id is None or tmdb_id is None
        if unidentified and movie.normalized_title == title:
            return True
    return False


PROBES = [
    (1, "dune"),          # id and title both match
    (1, "other"),         # id matches, title does not
    (2, "dune"),          # title matches, id does not
    (99, "unknown"),      # neither
    (None, "dune"),       # unidentified chart entry, title matches
    (None, "unknown"),    # unidentified, no match
    (None, "notitle"),    # matches the entry stored without a tmdb id
    (7, "notitle"),       # that entry's title, but a different id
    (2, "dune"),          # a REMAKE: identified, same title, different id
    (None, "dune"),       # unidentified probe against an identified stored entry
]


@pytest.mark.parametrize(("tmdb_id", "title"), PROBES)
def test_snapshot_matches_the_written_rule(
    store: IgnoreStore, tmdb_id: int | None, title: str
) -> None:
    """The set arithmetic must agree with the rule written out longhand."""
    store.add(tmdb_id=1, title="Dune", normalized_title="dune")
    store.add(tmdb_id=None, title="No Match Title", normalized_title="notitle")

    assert store.snapshot().is_ignored(tmdb_id, title) == _expected_is_ignored(
        store, tmdb_id, title
    )


def test_snapshot_reads_the_file_once_for_many_titles(
    store: IgnoreStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import filestore

    store.add(tmdb_id=1, title="Dune", normalized_title="dune")
    calls = [0]
    original = filestore.read_yaml

    def counting(*args: object, **kwargs: object) -> dict:
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(filestore, "read_yaml", counting)

    ignored = store.snapshot()
    for index in range(25):
        ignored.is_ignored(index, f"title-{index}")

    assert calls[0] == 1  # 25 titles, one read


def test_an_empty_list_ignores_nothing(store: IgnoreStore) -> None:
    ignored = store.snapshot()
    assert ignored.is_ignored(1, "anything") is False
    assert ignored.is_ignored(None, "anything") is False


def test_none_tmdb_id_never_matches_a_stored_id(store: IgnoreStore) -> None:
    # tmdb_ids must never contain None, or an unidentified chart entry would match
    # every entry stored without an id.
    store.add(tmdb_id=None, title="Untitled", normalized_title="untitled")
    assert None not in store.snapshot().tmdb_ids
    assert store.snapshot().is_ignored(None, "something else") is False


def test_snapshot_is_immutable(store: IgnoreStore) -> None:
    # Frozen + frozenset: a render holds it across 25 cards; nothing should be able to
    # edit the answer half-way down the page.
    ignored = store.snapshot()
    with pytest.raises(AttributeError):
        ignored.titles = frozenset({"tampered"})  # type: ignore[misc]
    assert isinstance(ignored, IgnoreSnapshot)


def test_store_is_ignored_still_works_for_single_lookups(store: IgnoreStore) -> None:
    # add()'s dedupe uses it; it delegates to the snapshot so the rule lives once.
    store.add(tmdb_id=5, title="Wicked", normalized_title="wicked")
    assert store.is_ignored(5, "anything") is True   # same film by id
    assert store.is_ignored(6, "wicked") is False    # same title, different film
    assert store.is_ignored(None, "wicked") is True  # nothing to compare but the title
    assert store.is_ignored(6, "nope") is False


def test_add_is_still_idempotent_through_the_delegated_rule(store: IgnoreStore) -> None:
    store.add(tmdb_id=5, title="Wicked", normalized_title="wicked")
    store.add(tmdb_id=5, title="Wicked", normalized_title="wicked")
    assert len(store.list_ignored()) == 1


def test_a_remake_is_not_ignored_because_the_original_was(store: IgnoreStore) -> None:
    """The bug this rule fixes: two films can normalize to the same title and still be
    different films. An id on both sides is proof they are, and it wins over the title."""
    store.add(tmdb_id=1234, title="Nosferatu", normalized_title="nosferatu")
    ignored = store.snapshot()

    assert ignored.is_ignored(1234, "nosferatu") is True   # the film that was ignored
    assert ignored.is_ignored(5678, "nosferatu") is False  # the 2026 remake, addable


def test_the_title_fallback_survives_for_films_radarr_could_not_identify(
    store: IgnoreStore,
) -> None:
    # The fallback's whole reason to exist: an entry stored with no id can only ever be
    # matched by title, so tightening the rule must not disable it.
    store.add(tmdb_id=None, title="Obscure Film", normalized_title="obscure film")
    ignored = store.snapshot()

    assert ignored.is_ignored(4242, "obscure film") is True
    assert ignored.is_ignored(None, "obscure film") is True
    assert ignored.is_ignored(4242, "something else") is False


def test_an_unidentified_chart_entry_still_matches_an_identified_ignore(
    store: IgnoreStore,
) -> None:
    # Radarr identified the film when it was ignored but cannot identify it this week.
    # With no id to compare, the admin's stated intent — never show me this — wins.
    store.add(tmdb_id=1234, title="Nosferatu", normalized_title="nosferatu")
    assert store.snapshot().is_ignored(None, "nosferatu") is True


def test_unidentified_titles_holds_only_id_less_entries(store: IgnoreStore) -> None:
    store.add(tmdb_id=1234, title="Nosferatu", normalized_title="nosferatu")
    store.add(tmdb_id=None, title="Obscure Film", normalized_title="obscure film")
    ignored = store.snapshot()

    assert ignored.unidentified_titles == frozenset({"obscure film"})
    assert ignored.titles == frozenset({"nosferatu", "obscure film"})


def test_dedupe_no_longer_swallows_a_different_film_with_the_same_title(
    store: IgnoreStore,
) -> None:
    """add() skips duplicates via the same rule, so tightening it also fixes the case
    where ignoring a remake was silently discarded as 'already ignored'."""
    store.add(tmdb_id=1234, title="Nosferatu", normalized_title="nosferatu")
    store.add(tmdb_id=5678, title="Nosferatu", normalized_title="nosferatu")

    assert len(store.list_ignored()) == 2
    assert {movie.tmdb_id for movie in store.list_ignored()} == {1234, 5678}
