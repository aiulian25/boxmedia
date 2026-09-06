"""TV step 12 integration test: the series detail and the add it exists for.

Two claims carry the weight. That the whole page arrives in ONE TMDB request — the
append_to_response bet the client made in step 6 — and that no Add can ever create the
wrong series or a second copy of the right one.
"""

from __future__ import annotations

import httpx
import respx

from app.services.discovery import TMDB_BASE_URL
from app.services.ignore import KIND_MOVIE, KIND_SERIES
from app.services.sonarr import SonarrSeries
from app.web.shows import ShowStatus
from tests.conftest import AppHarness

TMDB_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SONARR_URL = "http://127.0.0.1:2"
SONARR_KEY = "fedcba9876543210fedcba9876543210"
DETAIL_URL = f"{TMDB_BASE_URL}/tv/1396"
EXTERNAL_URL = f"{TMDB_BASE_URL}/tv/1396/external_ids"
SERIES_POST = f"{SONARR_URL}/api/v3/series"

DETAIL = {
    "id": 1396, "name": "The Hollow Coast", "first_air_date": "2023-01-20",
    "overview": "A survey team on a fault line that should not exist.",
    "poster_path": None, "backdrop_path": None, "vote_average": 8.4,
    "genres": [{"id": 18, "name": "Drama"}], "networks": [{"id": 49, "name": "HBO"}],
    "status": "Returning Series", "episode_run_time": [52],
    "number_of_seasons": 2, "number_of_episodes": 18, "in_production": True,
    "seasons": [
        {"season_number": 1, "name": "Season 1", "episode_count": 10,
         "air_date": "2023-01-20"},
        {"season_number": 2, "name": "Season 2", "episode_count": 8, "air_date": None},
    ],
    "external_ids": {"tvdb_id": 121361, "imdb_id": "tt0944947"},
    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
    "credits": {"cast": [{"name": "Ines Aldaz", "character": "Dr. Petra Kohl"}]},
    "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc"}]},
}


def _keys(harness: AppHarness) -> None:
    harness.client.app.state.discovery.save("tmdb", TMDB_KEY)


def _sonarr(harness: AppHarness) -> str:
    harness.client.app.state.apps.add(
        name="Sonarr", url=SONARR_URL, api_key=SONARR_KEY, kind="sonarr"
    )
    app_id = harness.client.app.state.apps.list_apps("sonarr")[0].id
    harness.client.app.state.apps.set_defaults(
        app_id, quality_profile_id=4, root_folder="/tv",
        series_type="anime", season_folders=True, search_on_add=False,
    )
    return app_id


def _mock_detail(payload: dict | None = None) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, json=payload if payload else DETAIL)
    )


# --- the page ---


@respx.mock
def test_the_whole_page_arrives_in_one_request(harness: AppHarness) -> None:
    """The append_to_response bet: seasons, cast, rating and the TVDB bridge all come
    with the show itself. The teardown found nzb360 fanning six calls out for this."""
    route = respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=DETAIL))
    harness.activate()
    _keys(harness)

    page = harness.client.get("/shows/1396").text

    assert route.call_count == 1
    assert "The Hollow Coast" in page
    assert "HBO" in page
    assert "TV-MA" in page
    assert "Ines Aldaz" in page
    assert "Season 1" in page
    assert "121361" in page


@respx.mock
def test_the_fragment_is_the_same_block_without_the_page(harness: AppHarness) -> None:
    """One source for the modal and the page, so the two can never drift."""
    _mock_detail()
    harness.activate()
    _keys(harness)

    fragment = harness.client.get("/shows/1396?fragment=1").text

    assert "The Hollow Coast" in fragment
    assert "<!DOCTYPE html>" not in fragment
    assert "<nav" not in fragment


@respx.mock
def test_season_dates_are_day_first_and_an_unannounced_one_says_so(
    harness: AppHarness,
) -> None:
    _mock_detail()
    harness.activate()
    _keys(harness)

    page = harness.client.get("/shows/1396").text

    assert "20/1/2023" in page
    # A season with no date renders words, not an empty cell.
    assert "Not announced" in page


def test_without_a_tmdb_key_the_page_points_at_settings(harness: AppHarness) -> None:
    """No respx mock: reaching TMDB here would fail on an unmocked call."""
    harness.activate()

    page = harness.client.get("/shows/1396").text
    assert "Add a TMDB API key" in page


@respx.mock
def test_an_unreachable_tmdb_says_so_rather_than_500ing(harness: AppHarness) -> None:
    respx.get(DETAIL_URL).mock(side_effect=httpx.ConnectError("gone"))
    harness.activate()
    _keys(harness)

    response = harness.client.get("/shows/1396")
    assert response.status_code == 200
    assert "could not be reached" in response.text
    # And the key never rides out on the error.
    assert TMDB_KEY not in response.text


# --- the add block asks one question ---


@respx.mock
def test_monitor_is_the_only_control_and_the_page_says_why(harness: AppHarness) -> None:
    """Quality, folder, series type and season folders are per connection. Asking them
    per title would turn one decision into five."""
    _mock_detail()
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    page = harness.client.get("/shows/1396").text

    assert 'name="monitor"' in page
    assert "All episodes" in page
    assert "First season only" in page
    assert "per connection, not per show" in page
    # None of the per-connection settings are offered here.
    for absent in ('name="quality_profile_id"', 'name="root_folder"',
                   'name="series_type"', 'name="season_folders"'):
        assert absent not in page


@respx.mock
def test_without_a_sonarr_the_page_says_where_to_add_one(harness: AppHarness) -> None:
    _mock_detail()
    harness.activate()
    _keys(harness)

    page = harness.client.get("/shows/1396").text
    assert "Add a Sonarr connection in" in page
    assert 'action="/add-series"' not in page


# --- the add itself ---


@respx.mock
def test_add_posts_the_connections_defaults_and_the_chosen_monitor(
    harness: AppHarness,
) -> None:
    _mock_detail()
    route = respx.post(SERIES_POST).mock(
        return_value=httpx.Response(201, json={"id": 14, "tvdbId": 121361,
                                               "title": "The Hollow Coast"})
    )
    respx.get(f"{SONARR_URL}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast",
        "tvdb_id": "121361", "monitor": "firstSeason",
    }, follow_redirects=False)

    assert ShowStatus.ADDED in response.headers["location"]
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["tvdbId"] == 121361
    # The connection's own settings, never the browser's.
    assert body["qualityProfileId"] == 4
    assert body["rootFolderPath"] == "/tv"
    assert body["seriesType"] == "anime"
    assert body["seasonFolder"] is True
    assert body["addOptions"]["searchForMissingEpisodes"] is False
    # The one thing the person chose.
    assert body["addOptions"]["monitor"] == "firstSeason"


@respx.mock
def test_a_successful_add_audits_and_refreshes_the_library(harness: AppHarness) -> None:
    _mock_detail()
    respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={"id": 14}))
    respx.get(f"{SONARR_URL}/api/v3/series").mock(return_value=httpx.Response(200, json=[
        {"id": 14, "tvdbId": 121361, "title": "The Hollow Coast", "year": 2023,
         "statistics": {"episodeCount": 18, "episodeFileCount": 0}},
    ]))
    harness.activate()
    _keys(harness)
    app_id = _sonarr(harness)

    harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast", "tvdb_id": "121361",
    }, follow_redirects=False)

    log = (harness.settings.logs_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "series_added" in log
    assert SONARR_KEY not in log
    # Re-read at once, so the card is right immediately rather than after the next
    # scheduled refresh.
    cached = harness.client.app.state.series_cache.load(app_id)
    assert cached is not None and len(cached[0]) == 1


@respx.mock
def test_a_series_already_in_sonarr_is_never_posted_twice(harness: AppHarness) -> None:
    """The guard is a snapshot lookup, never a string match on Sonarr's error body —
    tv-discovery.md §11 found exactly that resting on literal phrases."""
    _mock_detail()
    route = respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={"id": 14}))
    harness.activate()
    _keys(harness)
    _sonarr(harness)
    harness.client.app.state.series_cache.save("app-x", (
        SonarrSeries(
            sonarr_id=14, tvdb_id=121361, title="The Hollow Coast", year=2023,
            monitored=True, ended=False, episode_count=18, episode_file_count=18,
        ),
    ))

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast", "tvdb_id": "121361",
    }, follow_redirects=False)

    assert ShowStatus.ALREADY in response.headers["location"]
    assert not route.called, "a duplicate was posted to Sonarr"


@respx.mock
def test_a_tvdb_less_show_is_refused_and_offered_a_search(harness: AppHarness) -> None:
    """Sonarr identifies series by TVDB id. An Add that could not work is worse than
    saying so."""
    _mock_detail({**DETAIL, "external_ids": {"tvdb_id": None, "imdb_id": None}})
    respx.get(EXTERNAL_URL).mock(return_value=httpx.Response(200, json={"tvdb_id": None}))
    route = respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    page = harness.client.get("/shows/1396").text
    assert "No TVDB id" in page
    assert "Find it on TheTVDB" in page
    assert 'name="monitor"' not in page

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast", "tvdb_id": "",
    }, follow_redirects=False)

    assert ShowStatus.NO_TVDB in response.headers["location"]
    assert not route.called


@respx.mock
def test_a_tmdb_row_is_bridged_when_the_form_carries_no_id(harness: AppHarness) -> None:
    """A Trakt row posts its TVDB id for free; a TMDB row is resolved server-side."""
    bridge = respx.get(EXTERNAL_URL).mock(
        return_value=httpx.Response(200, json={"tvdb_id": 121361})
    )
    route = respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={"id": 14}))
    respx.get(f"{SONARR_URL}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast", "tvdb_id": "",
    }, follow_redirects=False)

    assert bridge.called
    assert ShowStatus.ADDED in response.headers["location"]
    import json
    assert json.loads(route.calls.last.request.content)["tvdbId"] == 121361


@respx.mock
def test_an_unknown_target_is_refused_rather_than_trusted(harness: AppHarness) -> None:
    """The browser chooses WHICH configured connection and nothing else, so no crafted
    form can aim an add at a host we do not have."""
    route = respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "X", "tvdb_id": "121361", "target": "app-elsewhere",
    }, follow_redirects=False)

    assert ShowStatus.ADD_CONFIG in response.headers["location"]
    assert not route.called


@respx.mock
def test_an_unknown_monitor_value_is_refused(harness: AppHarness) -> None:
    """A closed enum: the form offers four, and anything else is a bug or an attack."""
    route = respx.post(SERIES_POST).mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _keys(harness)
    _sonarr(harness)

    response = harness.client.post("/add-series", data={
        "tmdb_id": "1396", "title": "X", "tvdb_id": "121361", "monitor": "whenever",
    }, follow_redirects=False)

    assert ShowStatus.ADD_CONFIG in response.headers["location"]
    assert not route.called


def test_add_requires_this_sessions_csrf_token(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/add-series",
        data={"tmdb_id": "1396", "title": "X", "csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 403


# --- ignore, and the namespace collision it must not cause ---


@respx.mock
def test_ignoring_a_series_round_trips(harness: AppHarness) -> None:
    _mock_detail()
    harness.activate()
    _keys(harness)

    response = harness.client.post("/ignore-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast",
    }, follow_redirects=False)
    assert ShowStatus.IGNORED in response.headers["location"]

    store = harness.client.app.state.ignore
    assert store.is_ignored(1396, "hollow coast", KIND_SERIES) is True
    assert "Un-ignore" in harness.client.get("/shows/1396").text

    harness.client.post("/ignore-series", data={
        "tmdb_id": "1396", "title": "The Hollow Coast", "undo": "1",
    }, follow_redirects=False)
    assert store.is_ignored(1396, "hollow coast", KIND_SERIES) is False


def test_ignoring_a_series_never_hides_a_film_of_the_same_id(
    harness: AppHarness,
) -> None:
    """TMDB numbers films and series in SEPARATE namespaces — tv/1396 and movie/1396 are
    different titles. Without the kind marker, ignoring a show would quietly drop a film
    off every weekly chart."""
    harness.activate()
    store = harness.client.app.state.ignore

    store.add(tmdb_id=1396, title="The Hollow Coast", normalized_title="hollow coast",
              kind=KIND_SERIES)

    assert store.is_ignored(1396, "hollow coast", KIND_SERIES) is True
    assert store.is_ignored(1396, "hollow coast", KIND_MOVIE) is False
    # And the bare call — which every movie flow makes — still means films.
    assert store.is_ignored(1396, "hollow coast") is False


def test_un_ignoring_a_series_leaves_a_film_entry_alone(harness: AppHarness) -> None:
    harness.activate()
    store = harness.client.app.state.ignore
    store.add(tmdb_id=1396, title="A Film", normalized_title="a film")
    store.add(tmdb_id=1396, title="A Show", normalized_title="a film", kind=KIND_SERIES)

    store.remove(tmdb_id=1396, normalized_title="a film", kind=KIND_SERIES)

    assert store.is_ignored(1396, "a film") is True
    assert store.is_ignored(1396, "a film", KIND_SERIES) is False


def test_an_ignored_yml_from_before_television_loads_as_films(
    harness: AppHarness,
) -> None:
    """Additive field with a default: no schema bump, no migration."""
    harness.activate()
    (harness.settings.config_dir / "ignored.yml").write_text(
        "schema_version: 1\nignored:\n"
        "  - tmdb_id: 603\n    title: The Matrix\n    normalized_title: matrix\n",
        encoding="utf-8",
    )
    store = harness.client.app.state.ignore

    assert store.is_ignored(603, "matrix") is True
    assert store.is_ignored(603, "matrix", KIND_SERIES) is False


# --- the modal hook ---


@respx.mock
def test_a_discover_card_is_a_real_link_the_script_upgrades(
    harness: AppHarness,
) -> None:
    """The poster is a link to the same route, so with the script blocked the click
    simply navigates — the movie card's contract, kept for series."""
    from app.services.discovery import ANTICIPATED_KEY, TRENDING_KEY, DiscoverShow

    harness.activate()
    _keys(harness)
    harness.client.app.state.discovery.save("trakt", "t" * 40)
    harness.client.app.state.discover_cache.save({
        TRENDING_KEY: (DiscoverShow(
            tmdb_id=1396, tvdb_id=121361, imdb_id=None, trakt_id=1,
            title="The Hollow Coast", year=2023, overview=None, poster_url=None,
            watchers=10,
        ),),
        ANTICIPATED_KEY: (),
    })

    page = harness.client.get("/discover").text

    assert 'href="/shows/1396"' in page
    assert 'data-show="1396"' in page


def test_both_modal_hooks_share_one_handler() -> None:
    """A series opens in the same dialog as a film because it is the same gesture. Pinned
    in source for the reason the plural `querySelectorAll` above it is."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "app/static/js/app.js").read_text()
    assert 'closest("a[data-movie], a[data-show]")' in script


def test_an_unknown_ignore_kind_is_refused_rather_than_stored(
    harness: AppHarness,
) -> None:
    """Write-strict, like every other kind in this app. A stored value outside the closed
    set would sit in ignored.yml answering for neither medium — invisible to the film
    flow, invisible to the series flow, and impossible to remove from either."""
    import pytest

    harness.activate()
    store = harness.client.app.state.ignore

    with pytest.raises(ValueError, match="unknown ignore kind"):
        store.add(tmdb_id=1, title="X", normalized_title="x", kind="lidarr")

    assert store.list_ignored() == []


def test_a_stored_key_that_will_not_decrypt_says_so(harness: AppHarness) -> None:
    """"Add a TMDB API key" is the wrong advice for a key that is already there — the
    thing to fix is the encryption key, so the page says that instead."""
    from app.core import crypto
    from app.services.discovery import DiscoveryStore
    from app.web.shows import UNAVAILABLE_MESSAGE, UNREADABLE_KEY_MESSAGE

    harness.activate()
    _keys(harness)
    harness.client.app.state.discovery = DiscoveryStore(
        harness.settings.config_dir, key=crypto.generate_key()
    )

    page = harness.client.get("/shows/1396")

    assert page.status_code == 200
    assert UNREADABLE_KEY_MESSAGE in page.text
    assert UNAVAILABLE_MESSAGE not in page.text


def test_the_tmdb_client_ignores_the_home_server_tls_escape_hatch(tmp_path) -> None:
    """`BM_TLS_CA_FILE` is for the user's OWN servers, and a CA-file context trusts only
    that CA — so passing it to TMDB broke the show page on exactly the installs that set
    it, while Discover's refresh (which never passed it) kept working.

    Built adversarially: verification disabled AND a private CA named. The client must
    still verify against the system trust store, which is what the README promises for
    every public endpoint.
    """
    from types import SimpleNamespace

    from app.web.shows import _tmdb_client
    from tests.conftest import build_harness

    ca_file = tmp_path / "home-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nnot a real CA\n-----END CERTIFICATE-----\n")
    harness = build_harness(tmp_path, outbound_tls_verify=False, tls_ca_file=ca_file)
    harness.activate()
    _keys(harness)

    # The settings really are hostile, so this test cannot pass by accident.
    assert harness.settings.outbound_tls_verify is False
    assert harness.settings.tls_ca_file == ca_file

    client = _tmdb_client(SimpleNamespace(app=harness.client.app))

    assert client is not None
    assert client._verify is True
