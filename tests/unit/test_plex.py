"""Plex P1: the client, the two-tier snapshot, the store, and the cache."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core import crypto, filestore
from app.services.apps import API_KEY_MASK, InvalidAppError
from app.services.plex import (
    HOLDS_PROBABLY,
    HOLDS_YES,
    MAX_PLEX_PAGES,
    PLEX_CACHE_FILENAME,
    PLEX_PAGE_SIZE,
    PlexAuthError,
    PlexClient,
    PlexError,
    PlexFetch,
    PlexLibraryCache,
    PlexMovie,
    PlexStore,
    _movie_from_item,
    snapshot_from_movies,
)

PLEX_URL = "http://plex.local:32400"
TOKEN = "plex-token-for-tests"  # noqa: S105 — the suite's dummy token


def _client() -> PlexClient:
    return PlexClient(PLEX_URL, TOKEN)


def _sections_body() -> dict:
    return {"MediaContainer": {"Directory": [
        {"key": "1", "type": "movie", "title": "Movies"},
        {"key": "2", "type": "show", "title": "TV"},
        {"key": "5", "type": "movie", "title": "Kids Movies"},
    ]}}


def _movie_item(title: str, year: int | None, guids: list[str], legacy: str = "") -> dict:
    item: dict = {"title": title, "guid": legacy or f"plex://movie/{title}"}
    if year is not None:
        item["year"] = year
    if guids:
        item["Guid"] = [{"id": guid} for guid in guids]
    return item


# --- guid parsing: the three catalogue generations ---


def test_a_modern_item_yields_both_ids() -> None:
    movie = _movie_from_item(_movie_item(
        "Neon Rain", 2026, ["tmdb://52001", "imdb://tt5200001", "tvdb://999"],
    ))

    assert (movie.tmdb_id, movie.imdb_id) == (52001, "tt5200001")
    assert (movie.title, movie.year) == ("Neon Rain", 2026)


def test_a_legacy_agent_item_yields_its_imdb_id() -> None:
    movie = _movie_from_item(_movie_item(
        "Old Rip", 1998, [], legacy="com.plexapp.agents.imdb://tt0120338?lang=en",
    ))

    assert movie.imdb_id == "tt0120338"
    assert movie.tmdb_id is None


def test_an_item_with_no_usable_guid_still_contributes_its_title() -> None:
    movie = _movie_from_item(_movie_item("Home Video", 2020, []))

    assert (movie.tmdb_id, movie.imdb_id) == (None, None)
    assert movie.title == "Home Video"


def test_an_overlong_id_is_no_id_rather_than_a_truncated_one() -> None:
    """The M5 lesson, applied here before it can bite: a truncated imdb id is a
    DIFFERENT film's id, which is the exact false 'In Plex' this must never render."""
    movie = _movie_from_item(_movie_item("X", 2020, ["imdb://tt1234567890"]))

    assert movie.imdb_id is None


# --- the snapshot's two tiers ---


def _snapshot() -> object:
    return snapshot_from_movies((
        PlexMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id="tt5200001"),
        PlexMovie(title="Old Rip", year=1998, tmdb_id=None, imdb_id="tt0120338"),
        PlexMovie(title="Home Video", year=2020, tmdb_id=None, imdb_id=None),
        PlexMovie(title="Nosferatu", year=1922, tmdb_id=None, imdb_id=None),
        PlexMovie(title="Undated Thing", year=None, tmdb_id=None, imdb_id=None),
    ))


def test_a_tmdb_match_is_a_confident_yes() -> None:
    assert _snapshot().holds(52001, None, "Completely Different Spelling", 1900) == HOLDS_YES


def test_an_imdb_match_is_a_confident_yes() -> None:
    assert _snapshot().holds(None, "tt0120338", "Whatever", None) == HOLDS_YES


def test_a_title_and_year_match_is_only_probably() -> None:
    """The educated guess, and it says so: nothing verified this is the same film."""
    assert _snapshot().holds(None, None, "Home Video", 2020) == HOLDS_PROBABLY


def test_the_remake_trap_is_refused() -> None:
    """Plex holds the 1922 Nosferatu. The 2026 remake charts. Claiming coverage would
    cause the exact double-take this feature exists to prevent — from the other side."""
    assert _snapshot().holds(None, None, "Nosferatu", 2026) is None


def test_a_missing_year_on_either_side_still_allows_the_guess() -> None:
    # Chart card has no year:
    assert _snapshot().holds(None, None, "Home Video", None) == HOLDS_PROBABLY
    # Plex item has no year:
    assert _snapshot().holds(None, None, "Undated Thing", 2026) == HOLDS_PROBABLY


def test_an_unknown_film_is_simply_unknown() -> None:
    assert _snapshot().holds(None, None, "Never Heard Of It", 2026) is None


def test_titles_match_through_normalization_not_spelling() -> None:
    assert _snapshot().holds(None, None, "HOME VIDEO!", 2020) == HOLDS_PROBABLY


# --- the client: pagination, cap, honesty ---


def _page(items: list[dict], total: int) -> httpx.Response:
    return httpx.Response(200, json={"MediaContainer": {
        "Metadata": items, "size": len(items), "totalSize": total,
    }})


@respx.mock
async def test_every_movie_section_is_read_and_stitched() -> None:
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_body())
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=_page([_movie_item("Neon Rain", 2026, ["tmdb://52001"])], 1)
    )
    respx.get(f"{PLEX_URL}/library/sections/5/all").mock(
        return_value=_page([_movie_item("Kids Film", 2024, ["tmdb://52002"])], 1)
    )

    fetch = await _client().list_movies()

    assert {m.tmdb_id for m in fetch.movies} == {52001, 52002}
    assert fetch.truncated is False


@respx.mock
async def test_a_large_section_pages_until_complete() -> None:
    first = [_movie_item(f"A{i}", 2020, [f"tmdb://{1000 + i}"]) for i in range(PLEX_PAGE_SIZE)]
    second = [_movie_item("Last One", 2021, ["tmdb://9999"])]

    def _answer(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("X-Plex-Container-Start", 0))
        total = PLEX_PAGE_SIZE + 1
        return _page(first if start == 0 else second, total)

    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(
        200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    ))
    route = respx.get(f"{PLEX_URL}/library/sections/1/all").mock(side_effect=_answer)

    fetch = await _client().list_movies()

    assert len(fetch.movies) == PLEX_PAGE_SIZE + 1
    assert route.call_count == 2
    assert fetch.truncated is False


@respx.mock
async def test_the_page_cap_trims_and_says_so() -> None:
    """No silent caps: a library bigger than the bound reads as covered-with-a-caveat,
    never as covered."""
    endless = [_movie_item(f"B{i}", 2020, []) for i in range(PLEX_PAGE_SIZE)]
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(
        200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
    ))
    route = respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=_page(endless, PLEX_PAGE_SIZE * (MAX_PLEX_PAGES + 5))
    )

    fetch = await _client().list_movies()

    assert fetch.truncated is True
    assert route.call_count == MAX_PLEX_PAGES
    assert len(fetch.movies) == PLEX_PAGE_SIZE * MAX_PLEX_PAGES


@respx.mock
async def test_the_token_travels_as_a_header_never_in_the_url() -> None:
    """A token in a query string is a token in every access log on the path."""
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_sections_body())

    respx.get(f"{PLEX_URL}/library/sections").mock(side_effect=_capture)

    await _client().movie_section_keys()

    request = seen[0]
    assert request.headers["X-Plex-Token"] == TOKEN
    assert TOKEN not in str(request.url)


@respx.mock
async def test_a_rejected_token_is_its_own_error() -> None:
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))

    with pytest.raises(PlexAuthError):
        await _client().movie_section_keys()


@respx.mock
async def test_an_unreachable_server_raises_plex_error() -> None:
    respx.get(f"{PLEX_URL}/library/sections").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(PlexError):
        await _client().movie_section_keys()


# --- the store: encrypted at rest, masked in view ---


@pytest.fixture
def store(tmp_path: Path) -> PlexStore:
    return PlexStore(tmp_path, key=crypto.generate_key())


def test_the_token_never_touches_disk_in_plaintext(store: PlexStore, tmp_path: Path) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    raw = (tmp_path / "plex.yml").read_text(encoding="utf-8")
    assert TOKEN not in raw
    assert store.decrypt_token() == TOKEN


def test_the_public_view_masks_the_token(store: PlexStore) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    view = store.load().public()
    assert view == {"url": "http://plex.local:32400", "token_mask": API_KEY_MASK}


def test_a_blank_token_keeps_the_stored_one(store: PlexStore) -> None:
    """The Radarr cards' contract: editing the URL never forces re-pasting the secret."""
    store.save(url="http://plex.local:32400", token=TOKEN)

    store.save(url="https://plex.example", token=None)

    assert store.load().url == "https://plex.example"
    assert store.decrypt_token() == TOKEN


def test_a_first_save_without_a_token_is_refused(store: PlexStore) -> None:
    with pytest.raises(InvalidAppError):
        store.save(url="http://plex.local:32400", token=None)


def test_a_scheme_less_url_is_normalized_like_a_radarr_one(store: PlexStore) -> None:
    store.save(url="plex.local:32400", token=TOKEN)

    assert store.load().url.startswith("http://")


def test_remove_deletes_the_record(store: PlexStore) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    assert store.remove() is True
    assert store.load() is None
    assert store.remove() is False  # already gone is not an error


# --- the cache: a cache, not a record ---


def test_the_cache_round_trips_and_reports_its_age(tmp_path: Path) -> None:
    cache = PlexLibraryCache(tmp_path)
    cache.save(PlexFetch(movies=(
        PlexMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id="tt5200001"),
    ), truncated=True))

    loaded = cache.load()
    assert loaded is not None
    snapshot, fetched_at = loaded
    assert snapshot.holds(52001, None, "x", None) == HOLDS_YES
    assert snapshot.truncated is True
    assert fetched_at > 0


def test_an_unreadable_cache_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / PLEX_CACHE_FILENAME).write_text("{broken", encoding="utf-8")

    assert PlexLibraryCache(tmp_path).load() is None


def test_a_cache_from_a_newer_build_reads_as_empty(tmp_path: Path) -> None:
    filestore.write_json(
        tmp_path / PLEX_CACHE_FILENAME, {"movies": []},
        schema_version=99,
    )

    assert PlexLibraryCache(tmp_path).load() is None


def test_forget_drops_the_snapshot(tmp_path: Path) -> None:
    """A library nobody is connected to must not keep decorating cards."""
    cache = PlexLibraryCache(tmp_path)
    cache.save(PlexFetch(movies=(), truncated=False))

    cache.forget()

    assert cache.load() is None
    cache.forget()  # twice is fine


def test_the_cache_file_is_json_a_human_can_check(tmp_path: Path) -> None:
    cache = PlexLibraryCache(tmp_path)
    cache.save(PlexFetch(movies=(
        PlexMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    document = json.loads((tmp_path / PLEX_CACHE_FILENAME).read_text(encoding="utf-8"))
    assert document["movies"][0]["title"] == "Neon Rain"
