"""Plex P1: the client, the two-tier snapshot, the store, and the cache."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core import crypto, filestore
from app.services.apps import API_KEY_MASK, InvalidAppError
from app.services.mediaserver import (
    HOLDS_PROBABLY,
    HOLDS_YES,
    JELLYFIN_PAGE_SIZE,
    KIND_JELLYFIN,
    KIND_PLEX,
    LEGACY_PLEX_FILENAME,
    LIBRARY_CACHE_FILENAME,
    MAX_LIBRARY_PAGES,
    MEDIA_SERVER_FILENAME,
    MEDIA_SERVER_SCHEMA_VERSION,
    PLEX_PAGE_SIZE,
    JellyfinAuthError,
    JellyfinClient,
    MediaServerError,
    MediaServerFetch,
    MediaServerLibraryCache,
    MediaServerMovie,
    MediaServerSeries,
    MediaServerStore,
    PlexAuthError,
    PlexClient,
    PlexError,
    _movie_from_item,
    _movie_from_jellyfin_item,
    snapshot_from_library,
    validated_kind,
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
    return snapshot_from_library((
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id="tt5200001"),
        MediaServerMovie(title="Old Rip", year=1998, tmdb_id=None, imdb_id="tt0120338"),
        MediaServerMovie(title="Home Video", year=2020, tmdb_id=None, imdb_id=None),
        MediaServerMovie(title="Nosferatu", year=1922, tmdb_id=None, imdb_id=None),
        MediaServerMovie(title="Undated Thing", year=None, tmdb_id=None, imdb_id=None),
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
        return_value=_page(endless, PLEX_PAGE_SIZE * (MAX_LIBRARY_PAGES + 5))
    )

    fetch = await _client().list_movies()

    assert fetch.truncated is True
    assert route.call_count == MAX_LIBRARY_PAGES
    assert len(fetch.movies) == PLEX_PAGE_SIZE * MAX_LIBRARY_PAGES


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
def store(tmp_path: Path) -> MediaServerStore:
    return MediaServerStore(tmp_path, key=crypto.generate_key())


def test_the_token_never_touches_disk_in_plaintext(store: MediaServerStore, tmp_path: Path) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    raw = (tmp_path / MEDIA_SERVER_FILENAME).read_text(encoding="utf-8")
    assert TOKEN not in raw
    assert store.decrypt_token() == TOKEN


def test_the_public_view_masks_the_token(store: MediaServerStore) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    view = store.load().public()
    assert view == {
        "url": "http://plex.local:32400",
        "token_mask": API_KEY_MASK,
        "kind": KIND_PLEX,
        "name": "Plex",
        "secret_label": "Plex token",
    }


def test_a_blank_token_keeps_the_stored_one(store: MediaServerStore) -> None:
    """The Radarr cards' contract: editing the URL never forces re-pasting the secret."""
    store.save(url="http://plex.local:32400", token=TOKEN)

    store.save(url="https://plex.example", token=None)

    assert store.load().url == "https://plex.example"
    assert store.decrypt_token() == TOKEN


def test_a_first_save_without_a_token_is_refused(store: MediaServerStore) -> None:
    with pytest.raises(InvalidAppError):
        store.save(url="http://plex.local:32400", token=None)


def test_a_scheme_less_url_is_normalized_like_a_radarr_one(store: MediaServerStore) -> None:
    store.save(url="plex.local:32400", token=TOKEN)

    assert store.load().url.startswith("http://")


def test_remove_deletes_the_record(store: MediaServerStore) -> None:
    store.save(url="http://plex.local:32400", token=TOKEN)

    assert store.remove() is True
    assert store.load() is None
    assert store.remove() is False  # already gone is not an error


# --- the cache: a cache, not a record ---


def test_the_cache_round_trips_and_reports_its_age(tmp_path: Path) -> None:
    cache = MediaServerLibraryCache(tmp_path)
    cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id="tt5200001"),
    ), truncated=True))

    loaded = cache.load()
    assert loaded is not None
    snapshot, fetched_at = loaded
    assert snapshot.holds(52001, None, "x", None) == HOLDS_YES
    assert snapshot.truncated is True
    assert fetched_at > 0


def test_an_unreadable_cache_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / LIBRARY_CACHE_FILENAME).write_text("{broken", encoding="utf-8")

    assert MediaServerLibraryCache(tmp_path).load() is None


def test_a_cache_from_a_newer_build_reads_as_empty(tmp_path: Path) -> None:
    filestore.write_json(
        tmp_path / LIBRARY_CACHE_FILENAME, {"movies": []},
        schema_version=99,
    )

    assert MediaServerLibraryCache(tmp_path).load() is None


def test_forget_drops_the_snapshot(tmp_path: Path) -> None:
    """A library nobody is connected to must not keep decorating cards."""
    cache = MediaServerLibraryCache(tmp_path)
    cache.save(MediaServerFetch(movies=(), truncated=False))

    cache.forget()

    assert cache.load() is None
    cache.forget()  # twice is fine


def test_the_cache_file_is_json_a_human_can_check(tmp_path: Path) -> None:
    cache = MediaServerLibraryCache(tmp_path)
    cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    document = json.loads((tmp_path / LIBRARY_CACHE_FILENAME).read_text(encoding="utf-8"))
    assert document["movies"][0]["title"] == "Neon Rain"


# --- Jellyfin (J1) ---

JELLYFIN_URL = "http://jellyfin.local:8096"
API_KEY = "jellyfin-api-key-for-tests"  # noqa: S105 — the suite's dummy key


def _jf_client() -> JellyfinClient:
    return JellyfinClient(JELLYFIN_URL, API_KEY)


def _items(items: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "Items": items, "TotalRecordCount": total if total is not None else len(items),
    })


def _jf_item(name: str, year: int | None, providers: dict | None) -> dict:
    item: dict = {"Name": name}
    if year is not None:
        item["ProductionYear"] = year
    if providers is not None:
        item["ProviderIds"] = providers
    return item


def test_a_jellyfin_item_yields_both_ids() -> None:
    movie = _movie_from_jellyfin_item(
        _jf_item("Neon Rain", 2026, {"Imdb": "tt5200001", "Tmdb": "52001"})
    )

    assert (movie.tmdb_id, movie.imdb_id) == (52001, "tt5200001")
    assert (movie.title, movie.year) == ("Neon Rain", 2026)


def test_a_collection_id_is_never_taken_for_the_film() -> None:
    """The exact shape a live Jellyfin returned (demo.jellyfin.org, 10.11.11):
    ProviderIds carries TmdbCollection alongside Tmdb. Reading these the way the Plex
    client reads guids — a regex over the joined text — matches the COLLECTION and
    stamps it on the film, which is M5's wrong-poster failure arriving from a new
    direction. Ids are read by key equality for exactly this reason."""
    movie = _movie_from_jellyfin_item(_jf_item(
        "Caminandes: Gran Dillama", 2013,
        {"Imdb": "tt3434172", "Tmdb": "253774", "TmdbCollection": "339473"},
    ))

    assert movie.tmdb_id == 253774
    assert movie.tmdb_id != 339473


def test_provider_keys_are_matched_however_they_are_cased() -> None:
    movie = _movie_from_jellyfin_item(_jf_item("X", 2020, {"tmdb": "42", "IMDB": "tt1234567"}))

    assert (movie.tmdb_id, movie.imdb_id) == (42, "tt1234567")


def test_an_item_with_no_provider_ids_still_contributes_its_title() -> None:
    movie = _movie_from_jellyfin_item(_jf_item("Home Video", 2020, None))

    assert (movie.tmdb_id, movie.imdb_id) == (None, None)
    assert (movie.title, movie.year) == ("Home Video", 2020)


def test_a_jellyfin_id_outside_the_range_is_no_id() -> None:
    """The same bound the Plex side keeps: a truncated IMDb id is a different film's."""
    movie = _movie_from_jellyfin_item(_jf_item("X", 2020, {"Imdb": "tt1234567890"}))

    assert movie.imdb_id is None


def test_a_non_numeric_tmdb_value_is_no_id() -> None:
    movie = _movie_from_jellyfin_item(_jf_item("X", 2020, {"Tmdb": "not-a-number"}))

    assert movie.tmdb_id is None


@respx.mock
async def test_the_api_key_travels_as_a_header_never_in_the_url() -> None:
    """Jellyfin also accepts ?api_key= and we never use it: a credential in a URL is a
    credential in every access log on the path."""
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _items([], total=0)

    respx.get(f"{JELLYFIN_URL}/Items").mock(side_effect=_capture)

    await _jf_client().movie_section_keys()

    assert seen[0].headers["X-Emby-Token"] == API_KEY
    assert API_KEY not in str(seen[0].url)


@respx.mock
async def test_a_rejected_api_key_is_its_own_error() -> None:
    respx.get(f"{JELLYFIN_URL}/Items").mock(return_value=httpx.Response(401))

    with pytest.raises(JellyfinAuthError):
        await _jf_client().movie_section_keys()


@respx.mock
async def test_an_unreachable_jellyfin_raises_a_media_server_error() -> None:
    respx.get(f"{JELLYFIN_URL}/Items").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(MediaServerError):
        await _jf_client().movie_section_keys()


@respx.mock
async def test_an_empty_library_reads_as_nothing_to_read() -> None:
    """Reachable and authenticated with no movies is a FAILED test on either server —
    every later fetch would return nothing and the cards would never mention it."""
    respx.get(f"{JELLYFIN_URL}/Items").mock(return_value=_items([], total=0))

    assert await _jf_client().movie_section_keys() == []


@respx.mock
async def test_the_probe_asks_for_a_count_and_no_items() -> None:
    """`Limit=0` makes the Test button cost a count rather than the whole library."""
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _items([], total=5)

    respx.get(f"{JELLYFIN_URL}/Items").mock(side_effect=_capture)

    assert await _jf_client().movie_section_keys() == ["movies"]
    assert seen[0].url.params["Limit"] == "0"


@respx.mock
async def test_a_large_jellyfin_library_pages_until_complete() -> None:
    first = [_jf_item(f"A{i}", 2020, {"Tmdb": str(1000 + i)}) for i in range(JELLYFIN_PAGE_SIZE)]
    second = [_jf_item("Last One", 2021, {"Tmdb": "9999"})]
    total = JELLYFIN_PAGE_SIZE + 1

    def _answer(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("StartIndex", 0))
        return _items(first if start == 0 else second, total=total)

    route = respx.get(f"{JELLYFIN_URL}/Items").mock(side_effect=_answer)

    fetch = await _jf_client().list_movies()

    assert len(fetch.movies) == total
    assert route.call_count == 2
    assert fetch.truncated is False


@respx.mock
async def test_the_jellyfin_page_cap_trims_and_says_so() -> None:
    endless = [_jf_item(f"B{i}", 2020, None) for i in range(JELLYFIN_PAGE_SIZE)]
    route = respx.get(f"{JELLYFIN_URL}/Items").mock(
        return_value=_items(endless, total=JELLYFIN_PAGE_SIZE * (MAX_LIBRARY_PAGES + 5))
    )

    fetch = await _jf_client().list_movies()

    assert fetch.truncated is True
    assert route.call_count == MAX_LIBRARY_PAGES


@respx.mock
async def test_a_jellyfin_library_becomes_the_same_snapshot_a_plex_one_does() -> None:
    """The whole point of the rename: everything above the client is server-neutral."""
    respx.get(f"{JELLYFIN_URL}/Items").mock(return_value=_items([
        _jf_item("Neon Rain", 2026, {"Tmdb": "52001"}),
        _jf_item("Home Video", 2020, None),
    ]))

    fetch = await _jf_client().list_movies()
    snapshot = snapshot_from_library(fetch.movies)

    assert snapshot.holds(52001, None, "Spelled Differently", None) == HOLDS_YES
    assert snapshot.holds(None, None, "Home Video", 2020) == HOLDS_PROBABLY
    assert snapshot.holds(None, None, "Home Video", 1999) is None


# --- the kind on the one connection ---


def test_a_connection_defaults_to_plex(store: MediaServerStore) -> None:
    store.save(url=PLEX_URL, token=TOKEN)

    assert store.load().kind == KIND_PLEX
    assert store.load().name == "Plex"


def test_a_jellyfin_connection_names_itself_and_its_secret(store: MediaServerStore) -> None:
    store.save(url=JELLYFIN_URL, token=API_KEY, kind=KIND_JELLYFIN)

    view = store.load().public()
    assert view["kind"] == KIND_JELLYFIN
    assert view["name"] == "Jellyfin"
    assert view["secret_label"] == "API key"


def test_a_kind_this_build_does_not_ship_is_refused(store: MediaServerStore) -> None:
    with pytest.raises(InvalidAppError):
        store.save(url=PLEX_URL, token=TOKEN, kind="emby")


def test_a_blank_secret_may_not_cross_from_one_server_to_another(
    store: MediaServerStore,
) -> None:
    """Keeping the stored secret is a convenience for editing an address. Carrying it to
    a different server would send a Plex token to Jellyfin."""
    store.save(url=PLEX_URL, token=TOKEN)

    with pytest.raises(InvalidAppError):
        store.save(url=JELLYFIN_URL, token=None, kind=KIND_JELLYFIN)


def test_a_blank_secret_still_keeps_the_stored_one_for_the_same_server(
    store: MediaServerStore,
) -> None:
    store.save(url=PLEX_URL, token=TOKEN)

    store.save(url="https://plex.example", token=None, kind=KIND_PLEX)

    assert store.decrypt_token() == TOKEN


@pytest.mark.parametrize("stored", [None, "", "emby", 7, True])
def test_a_stored_kind_this_build_does_not_ship_reads_as_plex(stored: object) -> None:
    """Read-tolerant, write-strict. A file written before kinds existed has no kind at
    all and must load as what it is: a Plex connection."""
    assert validated_kind(stored) == KIND_PLEX


def test_a_pre_kinds_install_keeps_working(tmp_path: Path) -> None:
    """1.1.0 wrote plex.yml with no kind. It must load untouched, with no action."""
    key = crypto.generate_key()
    filestore.write_yaml(
        tmp_path / LEGACY_PLEX_FILENAME,
        {"server": {"url": PLEX_URL, "token_encrypted": crypto.encrypt_field(TOKEN, key)}},
        schema_version=MEDIA_SERVER_SCHEMA_VERSION,
    )
    store = MediaServerStore(tmp_path, key=key)

    connection = store.load()
    assert connection is not None
    assert connection.kind == KIND_PLEX
    assert store.decrypt_token() == TOKEN


def test_the_next_save_migrates_the_pre_kinds_file(tmp_path: Path) -> None:
    """Leaving both files would make the next load depend on which one it read."""
    key = crypto.generate_key()
    filestore.write_yaml(
        tmp_path / LEGACY_PLEX_FILENAME,
        {"server": {"url": PLEX_URL, "token_encrypted": crypto.encrypt_field(TOKEN, key)}},
        schema_version=MEDIA_SERVER_SCHEMA_VERSION,
    )
    store = MediaServerStore(tmp_path, key=key)

    store.save(url=PLEX_URL, token=None, kind=KIND_PLEX)

    assert (tmp_path / MEDIA_SERVER_FILENAME).exists()
    assert not (tmp_path / LEGACY_PLEX_FILENAME).exists()
    assert store.decrypt_token() == TOKEN


def test_removing_clears_the_pre_kinds_file_too(tmp_path: Path) -> None:
    key = crypto.generate_key()
    filestore.write_yaml(
        tmp_path / LEGACY_PLEX_FILENAME,
        {"server": {"url": PLEX_URL, "token_encrypted": crypto.encrypt_field(TOKEN, key)}},
        schema_version=MEDIA_SERVER_SCHEMA_VERSION,
    )
    store = MediaServerStore(tmp_path, key=key)

    assert store.remove() is True
    assert store.load() is None


# --- television (TV step 9) ---


def _show_item(title: str, year: int | None, guids: list[str], legacy: str = "") -> dict:
    item: dict = {"title": title, "guid": legacy or f"plex://show/{title}"}
    if year is not None:
        item["year"] = year
    if guids:
        item["Guid"] = [{"id": guid} for guid in guids]
    return item


def test_a_plex_show_reads_all_three_ids() -> None:
    from app.services.mediaserver import _series_from_item

    series = _series_from_item(
        _show_item("The Hollow Coast", 2023, [
            "tvdb://121361", "tmdb://1396", "imdb://tt0944947",
        ])
    )

    assert series.title == "The Hollow Coast"
    assert series.year == 2023
    # The one Sonarr keys on, so the one an "already on Plex" answer is most often about.
    assert series.tvdb_id == 121361
    assert series.tmdb_id == 1396
    assert series.imdb_id == "tt0944947"


def test_a_tvdb_guid_is_never_read_as_a_tmdb_one() -> None:
    """The two scheme names differ by a single letter. Reading one as the other would
    stamp a TVDB id into a TMDB field and match the wrong show entirely."""
    from app.services.mediaserver import _series_from_item

    series = _series_from_item(_show_item("Only TVDB", 2020, ["tvdb://121361"]))

    assert series.tvdb_id == 121361
    assert series.tmdb_id is None


def test_a_show_catalogued_by_the_tmdb_agent_still_lands() -> None:
    """A library with no TVDB id at all is normal, and the show is still held."""
    from app.services.mediaserver import _series_from_item

    series = _series_from_item(_show_item("TMDB Only", 2021, ["tmdb://1396"]))

    assert series.tvdb_id is None
    assert series.tmdb_id == 1396


def test_a_show_with_no_ids_contributes_title_and_year() -> None:
    from app.services.mediaserver import _series_from_item

    series = _series_from_item(_show_item("Home Recording", 2019, []))

    assert (series.tvdb_id, series.tmdb_id, series.imdb_id) == (None, None, None)
    assert (series.title, series.year) == ("Home Recording", 2019)


def test_a_jellyfin_series_reads_ids_by_key() -> None:
    from app.services.mediaserver import _series_from_jellyfin_item

    series = _series_from_jellyfin_item(_jf_item("The Hollow Coast", 2023, {
        "Tvdb": "121361", "Tmdb": "1396", "Imdb": "tt0944947",
    }))

    assert series.tvdb_id == 121361
    assert series.tmdb_id == 1396
    assert series.imdb_id == "tt0944947"


def test_the_tmdb_collection_trap_stays_fenced_for_series() -> None:
    """The M5 failure arriving from a new direction. A live Jellyfin answers with
    TmdbCollection beside Tmdb, and reading ids by searching the joined text — which is
    right for Plex's URI strings — would stamp the COLLECTION id onto the show. Fenced
    for movies already; series get the same fence from the same helper."""
    from app.services.mediaserver import _series_from_jellyfin_item

    series = _series_from_jellyfin_item(_jf_item("Trap", 2020, {
        "TmdbCollection": "999999", "Tvdb": "121361",
    }))

    assert series.tmdb_id is None, "the collection id was read as the show's id"
    assert series.tvdb_id == 121361


def test_the_movie_and_series_readers_share_one_fence() -> None:
    """One place to be right rather than two — the movie half taught us this trap."""
    from app.services.mediaserver import _movie_from_jellyfin_item, _series_from_jellyfin_item

    providers = {"TmdbCollection": "999999"}
    assert _movie_from_jellyfin_item(_jf_item("F", 2020, providers)).tmdb_id is None
    assert _series_from_jellyfin_item(_jf_item("S", 2020, providers)).tmdb_id is None


def test_an_overlong_series_imdb_id_is_no_id() -> None:
    from app.services.mediaserver import _series_from_jellyfin_item

    series = _series_from_jellyfin_item(_jf_item("X", 2020, {"Imdb": "tt1234567890"}))
    assert series.imdb_id is None


# --- the snapshot's two registers, for television ---


def _series_snapshot() -> object:
    return snapshot_from_library(
        (),
        series=(
            MediaServerSeries(
                title="The Hollow Coast", year=2023,
                tvdb_id=121361, tmdb_id=1396, imdb_id="tt0944947",
            ),
            MediaServerSeries(title="Sodium Lights", year=2024),
            MediaServerSeries(title="Undated Show", year=None),
        ),
    )


def test_a_tvdb_match_is_a_confident_yes() -> None:
    snapshot = _series_snapshot()
    assert snapshot.holds_series(121361, None, None, "Different Spelling", 1900) == HOLDS_YES


def test_a_series_tmdb_or_imdb_match_is_also_confident() -> None:
    snapshot = _series_snapshot()
    assert snapshot.holds_series(None, 1396, None, "x", None) == HOLDS_YES
    assert snapshot.holds_series(None, None, "tt0944947", "x", None) == HOLDS_YES


def test_a_series_title_and_year_match_is_the_amber_guess() -> None:
    snapshot = _series_snapshot()
    assert snapshot.holds_series(None, None, None, "sodium lights", 2024) == HOLDS_PROBABLY


def test_a_conflicting_year_is_not_a_series_match() -> None:
    """The reboot trap: claiming a 2024 show covers a 2031 revival would cause the exact
    double-take this exists to prevent."""
    snapshot = _series_snapshot()
    assert snapshot.holds_series(None, None, None, "Sodium Lights", 2031) is None


def test_a_series_with_no_year_matches_any_asked_year() -> None:
    """Absence of evidence is not a conflicting year."""
    snapshot = _series_snapshot()
    assert snapshot.holds_series(None, None, None, "Undated Show", 2031) == HOLDS_PROBABLY


def test_a_series_the_server_does_not_hold_is_none() -> None:
    snapshot = _series_snapshot()
    assert snapshot.holds_series(99, 99, "tt9999999", "Nothing Like It", 2020) is None


def test_films_and_series_never_answer_for_each_other() -> None:
    """A film and a show can share a title AND a TMDB id namespace. Answering a series
    question from the movie registers would render "already on Plex" over a show nobody
    owns."""
    snapshot = snapshot_from_library(
        (MediaServerMovie(title="Fargo", year=1996, tmdb_id=275, imdb_id="tt0116282"),),
        series=(MediaServerSeries(title="Sodium Lights", year=2024, tvdb_id=7),),
    )

    assert snapshot.holds_series(None, 275, None, "Fargo", 1996) is None
    assert snapshot.holds(None, None, "Sodium Lights", 2024) is None
    # Each still answers its own.
    assert snapshot.holds(275, None, "x", None) == HOLDS_YES
    assert snapshot.holds_series(7, None, None, "x", None) == HOLDS_YES


# --- listing shows ---


@respx.mock
async def test_only_show_sections_are_read_for_series() -> None:
    """Plex's type=2 is a show, the way type=1 is a movie. Asking a TV section for
    type=1 returns nothing rather than erroring — silence that reads as "you own no
    television"."""
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_body())
    )
    route = respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=_page([_show_item("The Hollow Coast", 2023, ["tvdb://121361"])], 1)
    )

    series, truncated = await _client().list_shows()

    assert [s.title for s in series] == ["The Hollow Coast"]
    assert truncated is False
    assert route.calls.last.request.url.params["type"] == "2"


@respx.mock
async def test_a_library_read_stitches_films_and_shows() -> None:
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_body())
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=_page([_movie_item("Neon Rain", 2026, ["tmdb://52001"])], 1)
    )
    respx.get(f"{PLEX_URL}/library/sections/5/all").mock(return_value=_page([], 0))
    respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=_page([_show_item("The Hollow Coast", 2023, ["tvdb://121361"])], 1)
    )

    fetch = await _client().list_library()

    assert [m.title for m in fetch.movies] == ["Neon Rain"]
    assert [s.title for s in fetch.series] == ["The Hollow Coast"]


@respx.mock
async def test_a_movies_only_install_never_walks_the_tv_library() -> None:
    """Reading a whole TV library every fifteen minutes to answer a question nobody on
    this install is asking is a cost that shows up as a slow page on someone's NAS."""
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_body())
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(return_value=_page([], 0))
    respx.get(f"{PLEX_URL}/library/sections/5/all").mock(return_value=_page([], 0))
    shows = respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=_page([], 0)
    )

    fetch = await _client().list_library(include_series=False)

    assert fetch.series == ()
    assert not shows.called


@respx.mock
async def test_a_trimmed_show_list_makes_the_whole_answer_partial() -> None:
    """The card that says "truncated" must not claim completeness because the other half
    fitted."""
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_body())
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(return_value=_page([], 0))
    respx.get(f"{PLEX_URL}/library/sections/5/all").mock(return_value=_page([], 0))
    respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=_page(
            [_show_item(f"S{i}", 2020, []) for i in range(PLEX_PAGE_SIZE)], 10**6
        )
    )

    fetch = await _client().list_library()
    assert fetch.truncated is True


@respx.mock
async def test_jellyfin_asks_for_series_not_episodes() -> None:
    """A series is one item with its episodes beneath it. Asking for Episode would bring
    a 200-episode show back as 200 rows."""
    route = respx.get(f"{JELLYFIN_URL}/Items").mock(
        return_value=_items([_jf_item("The Hollow Coast", 2023, {"Tvdb": "121361"})])
    )

    series, truncated = await _jf_client().list_shows()

    assert [s.tvdb_id for s in series] == [121361]
    assert truncated is False
    assert route.calls.last.request.url.params["IncludeItemTypes"] == "Series"


@respx.mock
async def test_jellyfin_movies_still_ask_for_movies() -> None:
    """The regression half of routing both through one pager."""
    route = respx.get(f"{JELLYFIN_URL}/Items").mock(
        return_value=_items([_jf_item("Neon Rain", 2026, {"Tmdb": "52001"})])
    )
    fetch = await _jf_client().list_movies()

    assert [m.tmdb_id for m in fetch.movies] == [52001]
    assert route.calls.last.request.url.params["IncludeItemTypes"] == "Movie"


# --- the cache: additive, so yesterday's file still loads ---


def test_a_cache_written_before_television_loads_fine(tmp_path: Path) -> None:
    """Additive field with a default, so no schema bump and no migration — this
    project's own convention. An existing install must not lose its movie snapshot
    because television arrived."""
    import json

    from app.services.mediaserver import LIBRARY_CACHE_FILENAME, MediaServerLibraryCache

    (tmp_path / LIBRARY_CACHE_FILENAME).write_text(
        json.dumps({
            "schema_version": 1,
            "fetched_at": 1.0,
            "truncated": False,
            "movies": [{"title": "Neon Rain", "year": 2026, "tmdb_id": 52001,
                        "imdb_id": "tt5200001"}],
        }),
        encoding="utf-8",
    )

    loaded = MediaServerLibraryCache(tmp_path).load()
    assert loaded is not None
    snapshot, _ = loaded

    assert snapshot.holds(52001, None, "x", None) == HOLDS_YES
    # No series key at all reads as no series, not as an error.
    assert snapshot.holds_series(1, 1, "tt1", "Anything", 2020) is None


def test_series_round_trip_through_the_cache(tmp_path: Path) -> None:
    from app.services.mediaserver import MediaServerLibraryCache

    cache = MediaServerLibraryCache(tmp_path)
    cache.save(MediaServerFetch(
        movies=(),
        truncated=False,
        series=(MediaServerSeries(
            title="The Hollow Coast", year=2023, tvdb_id=121361, tmdb_id=1396,
        ),),
    ))

    snapshot, _ = cache.load()
    assert snapshot.holds_series(121361, None, None, "x", None) == HOLDS_YES
    # Both registers survive the round trip, each still saying what it means.
    assert snapshot.holds_series(None, None, None, "the hollow coast", 2023) == HOLDS_PROBABLY
