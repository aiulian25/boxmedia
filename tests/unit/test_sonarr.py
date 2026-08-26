"""TV step 3 test: Sonarr API behavior via respx + real TLS validation via trustme.

Deliberately the same harness as test_radarr.py — respx for the contract, a throwaway
HTTPS server for the TLS posture — so the two clients are held to one standard.
"""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import respx
import trustme

from app.services.sonarr import (
    MONITOR_ALL,
    MONITOR_FIRST_SEASON,
    QUEUE_PAGE_SIZE,
    SonarrAuthError,
    SonarrClient,
    SonarrConnectionError,
    SonarrError,
    build_verify,
)

BASE_URL = "http://sonarr.local:8989"
API = f"{BASE_URL}/api/v3"

# One /series entry, trimmed to the fields the client reads.
SERIES_PAYLOAD = {
    "id": 14,
    "tvdbId": 121361,
    "tmdbId": 1399,
    "imdbId": "tt0944947",
    "title": "The Hollow Coast",
    "year": 2023,
    "monitored": True,
    "ended": False,
    "status": "continuing",
    "path": "/tv/The Hollow Coast",
    "titleSlug": "the-hollow-coast",
    "qualityProfileId": 4,
    "images": [
        {"coverType": "poster", "url": "/MediaCover/14/poster.jpg",
         "remoteUrl": "https://artworks.example/poster.jpg"},
    ],
    "statistics": {"episodeCount": 34, "episodeFileCount": 26, "sizeOnDisk": 1024},
    "seasons": [
        {"seasonNumber": 1, "monitored": True,
         "statistics": {"episodeCount": 10, "episodeFileCount": 10, "sizeOnDisk": 512}},
        {"seasonNumber": 2, "monitored": True,
         "statistics": {"episodeCount": 24, "episodeFileCount": 16, "sizeOnDisk": 512}},
    ],
}


# --- API behavior (respx) ---


@respx.mock
async def test_system_status_returns_json() -> None:
    respx.get(f"{API}/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    client = SonarrClient(BASE_URL, "apikey")
    status = await client.system_status()
    # The caller compares appName against APP_NAME — Radarr answers this path with the
    # same shape, so the name is the only thing that says which app replied.
    assert status["appName"] == "Sonarr"
    assert status["version"] == "4.0.1"


@respx.mock
async def test_the_api_key_travels_as_a_header_never_in_the_url() -> None:
    route = respx.get(f"{API}/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr"})
    )
    await SonarrClient(BASE_URL, "s3cr3t-key").system_status()

    request = route.calls.last.request
    assert request.headers["X-Api-Key"] == "s3cr3t-key"
    # A URL reaches proxy logs and browser history; this one opens a download pipeline.
    assert "s3cr3t-key" not in str(request.url)


@respx.mock
async def test_list_series_maps_fields_including_seasons() -> None:
    respx.get(f"{API}/series").mock(return_value=httpx.Response(200, json=[SERIES_PAYLOAD]))

    series = (await SonarrClient(BASE_URL, "apikey").list_series())[0]

    assert series.sonarr_id == 14
    # The identity that matters: Sonarr keys on TVDB, not TMDB.
    assert series.tvdb_id == 121361
    assert series.tmdb_id == 1399
    assert series.imdb_id == "tt0944947"
    assert series.title == "The Hollow Coast"
    assert series.year == 2023
    assert series.monitored is True
    assert series.ended is False
    assert (series.episode_count, series.episode_file_count) == (34, 26)
    assert series.path == "/tv/The Hollow Coast"
    assert series.quality_profile_id == 4
    # remoteUrl wins: `url` is a path on the Sonarr host, unreachable from a browser
    # pointed at BoxMedia.
    assert series.poster_url == "https://artworks.example/poster.jpg"
    assert [season.season_number for season in series.seasons] == [1, 2]
    assert series.seasons[0].complete is True
    assert series.seasons[1].complete is False


async def test_missing_episode_count_is_never_negative() -> None:
    """Sonarr counts specials in some totals and not others; a card reading
    "-2 missing" would be nonsense."""
    from app.services.sonarr import _library_series

    series = _library_series(
        {**SERIES_PAYLOAD, "statistics": {"episodeCount": 10, "episodeFileCount": 12}}
    )
    assert series.missing_episode_count == 0
    assert series.complete is True


async def test_a_series_with_no_aired_episodes_is_not_complete() -> None:
    """An announced show has 0 of 0 files. Calling that "complete" would put a green
    tick on something nobody can watch."""
    from app.services.sonarr import _library_series

    series = _library_series(
        {**SERIES_PAYLOAD, "statistics": {"episodeCount": 0, "episodeFileCount": 0}}
    )
    assert series.complete is False


@respx.mock
async def test_ended_falls_back_to_the_status_word() -> None:
    respx.get(f"{API}/series").mock(
        return_value=httpx.Response(
            200, json=[{**SERIES_PAYLOAD, "ended": None, "status": "ended"}]
        )
    )
    series = (await SonarrClient(BASE_URL, "apikey").list_series())[0]
    assert series.ended is True


@respx.mock
async def test_series_by_tvdb_returns_none_when_sonarr_does_not_have_it() -> None:
    respx.get(f"{API}/series").mock(return_value=httpx.Response(200, json=[]))
    assert await SonarrClient(BASE_URL, "apikey").series_by_tvdb(121361) is None


@respx.mock
async def test_series_by_tvdb_ignores_an_answer_for_a_different_show() -> None:
    """An older Sonarr ignores an unrecognised filter and returns the whole library.
    Trusting the first row would report the wrong series as already added."""
    respx.get(f"{API}/series").mock(
        return_value=httpx.Response(200, json=[{**SERIES_PAYLOAD, "tvdbId": 999}])
    )
    assert await SonarrClient(BASE_URL, "apikey").series_by_tvdb(121361) is None


@respx.mock
async def test_series_by_tvdb_returns_the_match() -> None:
    respx.get(f"{API}/series").mock(return_value=httpx.Response(200, json=[SERIES_PAYLOAD]))
    found = await SonarrClient(BASE_URL, "apikey").series_by_tvdb(121361)
    assert found is not None and found.sonarr_id == 14


# --- queue: episode records folded to one figure per series ---


@respx.mock
async def test_queue_folds_episodes_into_one_figure_per_series() -> None:
    """Sonarr queues EPISODES but a card shows a SERIES. The lowest wins: a series is no
    nearer than its slowest episode, and 98% beside a 10% episode would be a lie about
    when it is watchable."""
    respx.get(f"{API}/queue").mock(
        return_value=httpx.Response(200, json={"records": [
            {"seriesId": 14, "episodeId": 1, "size": 100, "sizeleft": 2},   # 98%
            {"seriesId": 14, "episodeId": 2, "size": 100, "sizeleft": 90},  # 10%
            {"seriesId": 22, "episodeId": 3, "size": 100, "sizeleft": 50},  # 50%
        ]})
    )
    progress = await SonarrClient(BASE_URL, "apikey").queue()
    # approx because the client returns the raw float, exactly as Radarr's does — the
    # template rounds for display, and diverging the two clients to tidy 9.999…% would
    # buy nothing a viewer could see.
    assert progress == pytest.approx({14: 10.0, 22: 50.0})


@respx.mock
async def test_queue_asks_for_one_page() -> None:
    route = respx.get(f"{API}/queue").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    await SonarrClient(BASE_URL, "apikey").queue()
    assert route.calls.last.request.url.params["pageSize"] == str(QUEUE_PAGE_SIZE)


@respx.mock
async def test_queue_survives_unsized_and_malformed_records() -> None:
    respx.get(f"{API}/queue").mock(
        return_value=httpx.Response(200, json={"records": [
            {"seriesId": 14, "size": 0, "sizeleft": 0},          # not sized yet -> 0%
            {"seriesId": 22, "size": "big", "sizeleft": 1},      # unusable, skipped
            {"seriesId": True, "size": 100, "sizeleft": 1},      # bool is not an id
            "not-a-record",
            {"seriesId": 31, "size": 100, "sizeleft": 120},      # revised estimate
        ]})
    )
    progress = await SonarrClient(BASE_URL, "apikey").queue()
    assert progress == {14: 0.0, 31: 0.0}  # never a negative fill, never a ZeroDivision


@respx.mock
async def test_queue_rejects_a_surprising_shape() -> None:
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json=["nope"]))
    with pytest.raises(SonarrError):
        await SonarrClient(BASE_URL, "apikey").queue()


# --- lookup ---


@respx.mock
async def test_lookup_by_tvdb_prefers_the_id_term() -> None:
    route = respx.get(f"{API}/series/lookup").mock(
        return_value=httpx.Response(200, json=[{
            "tvdbId": 121361, "title": "The Hollow Coast", "year": 2023,
            "overview": "…", "status": "continuing", "network": "HBO",
            "imdbId": "tt0944947",
            "images": [{"coverType": "poster", "remoteUrl": "https://a.example/p.jpg"}],
        }])
    )
    result = await SonarrClient(BASE_URL, "apikey").lookup_by_tvdb(121361)

    assert route.calls.last.request.url.params["term"] == "tvdb:121361"
    assert result is not None
    assert (result.tvdb_id, result.title, result.network) == (121361, "The Hollow Coast", "HBO")


@respx.mock
async def test_lookup_by_tvdb_is_none_when_sonarr_cannot_resolve_the_id() -> None:
    """The honest answer for a show TMDB gave a TVDB id for that Sonarr's metadata does
    not know — the add flow refuses rather than posting a guess."""
    respx.get(f"{API}/series/lookup").mock(return_value=httpx.Response(200, json=[]))
    assert await SonarrClient(BASE_URL, "apikey").lookup_by_tvdb(121361) is None


@respx.mock
async def test_lookup_by_tvdb_rejects_a_mismatched_answer() -> None:
    respx.get(f"{API}/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 42, "title": "Something Else"}])
    )
    assert await SonarrClient(BASE_URL, "apikey").lookup_by_tvdb(121361) is None


@respx.mock
async def test_lookup_accepts_a_plain_title_term() -> None:
    route = respx.get(f"{API}/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 1, "title": "Verdigris"}])
    )
    results = await SonarrClient(BASE_URL, "apikey").lookup("Verdigris")
    assert route.calls.last.request.url.params["term"] == "Verdigris"
    assert results[0].title == "Verdigris"


# --- add ---


@respx.mock
async def test_add_series_posts_the_body_sonarr_v3_expects() -> None:
    route = respx.post(f"{API}/series").mock(
        return_value=httpx.Response(201, json=SERIES_PAYLOAD)
    )

    added = await SonarrClient(BASE_URL, "apikey").add_series(
        tvdb_id=121361,
        title="The Hollow Coast",
        quality_profile_id=4,
        root_folder_path="/tv",
        monitor=MONITOR_FIRST_SEASON,
        season_folder=True,
        series_type="anime",
        search_on_add=False,
    )

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "tvdbId": 121361,
        "title": "The Hollow Coast",
        "qualityProfileId": 4,
        "rootFolderPath": "/tv",
        "monitored": True,
        "seasonFolder": True,
        "seriesType": "anime",
        "addOptions": {
            "monitor": "firstSeason",
            "searchForMissingEpisodes": False,
            "searchForCutoffUnmetEpisodes": False,
        },
    }
    # The response is parsed by the same reader the library list uses, so the two can
    # never drift in what they read.
    assert added.sonarr_id == 14
    assert added.tvdb_id == 121361


@respx.mock
async def test_adding_unmonitored_still_monitors_the_series_itself() -> None:
    """Sonarr's own behaviour, and what lets someone monitor a season later without
    re-adding the show."""
    route = respx.post(f"{API}/series").mock(
        return_value=httpx.Response(201, json=SERIES_PAYLOAD)
    )
    await SonarrClient(BASE_URL, "apikey").add_series(
        tvdb_id=1, title="X", quality_profile_id=1, root_folder_path="/tv", monitor="none"
    )
    body = json.loads(route.calls.last.request.content)
    assert body["monitored"] is True
    assert body["addOptions"]["monitor"] == "none"


@respx.mock
async def test_add_series_refuses_an_unknown_monitor_option() -> None:
    """Refused HERE, before anything reaches Sonarr — so the route must stay uncalled.

    Matching the message and asserting that matters: a bare `pytest.raises(SonarrError)`
    passes even with the guard removed, because the escaped call fails DNS and raises
    SonarrConnectionError, which is a SonarrError too.
    """
    route = respx.post(f"{API}/series").mock(
        return_value=httpx.Response(201, json=SERIES_PAYLOAD)
    )
    with pytest.raises(SonarrError, match="unknown monitor option"):
        await SonarrClient(BASE_URL, "apikey").add_series(
            tvdb_id=1, title="X", quality_profile_id=1,
            root_folder_path="/tv", monitor="whenever",
        )
    assert not route.called


@respx.mock
async def test_add_series_refuses_an_unknown_series_type() -> None:
    route = respx.post(f"{API}/series").mock(
        return_value=httpx.Response(201, json=SERIES_PAYLOAD)
    )
    with pytest.raises(SonarrError, match="unknown series type"):
        await SonarrClient(BASE_URL, "apikey").add_series(
            tvdb_id=1, title="X", quality_profile_id=1,
            root_folder_path="/tv", series_type="cartoons",
        )
    assert not route.called


# --- errors ---


@respx.mock
async def test_unauthorized_maps_to_auth_error() -> None:
    respx.get(f"{API}/system/status").mock(return_value=httpx.Response(401))
    with pytest.raises(SonarrAuthError):
        await SonarrClient(BASE_URL, "apikey").system_status()


@respx.mock
async def test_forbidden_maps_to_auth_error() -> None:
    respx.get(f"{API}/system/status").mock(return_value=httpx.Response(403))
    with pytest.raises(SonarrAuthError):
        await SonarrClient(BASE_URL, "apikey").system_status()


@respx.mock
async def test_connect_failure_maps_to_connection_error() -> None:
    respx.get(f"{API}/system/status").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SonarrConnectionError):
        await SonarrClient(BASE_URL, "apikey").system_status()


@respx.mock
async def test_a_refused_add_carries_sonarrs_own_words() -> None:
    """Shown, never matched. nzb360 keyed duplicate detection off the literal strings in
    this body (tv-discovery.md §11), so any rewording degraded it to an unknown failure.
    We surface the sentence and let the snapshot check decide what it means."""
    respx.post(f"{API}/series").mock(
        return_value=httpx.Response(400, json=[
            {"errorMessage": "This series has already been added", "propertyName": "TvdbId"}
        ])
    )
    with pytest.raises(SonarrError) as raised:
        await SonarrClient(BASE_URL, "apikey").add_series(
            tvdb_id=121361, title="X", quality_profile_id=1, root_folder_path="/tv"
        )
    assert raised.value.detail == "This series has already been added"
    assert "400" in str(raised.value)


@respx.mock
async def test_an_error_detail_is_bounded_and_stripped_of_control_characters() -> None:
    """Remote-supplied text on its way to a banner and an audit line: it must not be able
    to fill a log or forge a line in one."""
    respx.post(f"{API}/series").mock(
        return_value=httpx.Response(400, json={"message": "bad\n\r\tnews " + "x" * 500})
    )
    with pytest.raises(SonarrError) as raised:
        await SonarrClient(BASE_URL, "apikey").add_series(
            tvdb_id=1, title="X", quality_profile_id=1, root_folder_path="/tv"
        )
    detail = raised.value.detail
    assert detail is not None
    assert len(detail) <= 200
    assert "\n" not in detail and "\r" not in detail and "\t" not in detail


@respx.mock
async def test_an_error_without_a_body_still_raises_cleanly() -> None:
    respx.post(f"{API}/series").mock(return_value=httpx.Response(500, text="<html>oops"))
    with pytest.raises(SonarrError) as raised:
        await SonarrClient(BASE_URL, "apikey").add_series(
            tvdb_id=1, title="X", quality_profile_id=1, root_folder_path="/tv"
        )
    assert raised.value.detail is None


@respx.mock
async def test_a_non_json_200_maps_to_sonarr_error() -> None:
    """A proxy in front of Sonarr can answer any path with an HTML login page.
    JSONDecodeError would escape every caller's except and become a 500."""
    respx.get(f"{API}/series").mock(
        return_value=httpx.Response(200, text="<html>login</html>")
    )
    with pytest.raises(SonarrError):
        await SonarrClient(BASE_URL, "apikey").list_series()


@respx.mock
async def test_a_dict_where_a_list_belongs_is_refused() -> None:
    """A bare `for item in ...` over a dict silently yields its KEYS — wrong data rather
    than a caught error."""
    respx.get(f"{API}/series").mock(return_value=httpx.Response(200, json={"a": 1}))
    with pytest.raises(SonarrError):
        await SonarrClient(BASE_URL, "apikey").list_series()


# --- calendar ---


@respx.mock
async def test_calendar_maps_episodes_with_their_series() -> None:
    route = respx.get(f"{API}/calendar").mock(
        return_value=httpx.Response(200, json=[{
            "id": 501, "seriesId": 14, "seasonNumber": 3, "episodeNumber": 7,
            "title": "Low Tide", "airDateUtc": "2026-08-28T21:00:00Z",
            "hasFile": False, "monitored": True,
            "series": {"title": "The Hollow Coast", "tvdbId": 121361},
        }])
    )

    start = datetime(2026, 8, 24, tzinfo=UTC)
    end = datetime(2026, 8, 31, tzinfo=UTC)
    episodes = await SonarrClient(BASE_URL, "apikey").calendar(start, end)

    params = route.calls.last.request.url.params
    # includeSeries so one call renders a whole week — otherwise every row needs a
    # second request for the title, and a page load waits on a slow Sonarr.
    assert params["includeSeries"] == "true"
    assert params["start"] == start.isoformat()

    episode = episodes[0]
    assert (episode.episode_id, episode.series_id) == (501, 14)
    assert episode.series_title == "The Hollow Coast"
    assert episode.tvdb_id == 121361
    assert (episode.season_number, episode.episode_number) == (3, 7)
    assert episode.air_date_utc == datetime(2026, 8, 28, 21, 0, tzinfo=UTC)
    assert episode.has_file is False


@respx.mock
async def test_a_malformed_episode_does_not_take_down_the_week() -> None:
    respx.get(f"{API}/calendar").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "seriesId": 14, "seasonNumber": 1},          # no episodeNumber
            "not-an-episode",
            {"id": 2, "seriesId": 14, "seasonNumber": 1, "episodeNumber": 2,
             "airDateUtc": "not a date", "series": {"title": "X"}},
        ])
    )
    episodes = await SonarrClient(BASE_URL, "apikey").calendar(
        datetime(2026, 8, 24, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
    )
    assert [episode.episode_id for episode in episodes] == [2]
    # An unparseable air date is None, not an exception: one bad row must not blank a week.
    assert episodes[0].air_date_utc is None


# --- options for the Settings dropdowns ---


@respx.mock
async def test_quality_profiles_and_root_folders() -> None:
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/tv"}])
    )
    client = SonarrClient(BASE_URL, "apikey")
    assert await client.quality_profiles() == [(4, "HD-1080p")]
    assert await client.root_folders() == ["/tv"]


@respx.mock
async def test_a_profile_without_a_name_is_refused() -> None:
    """This feeds a Settings dropdown, not a page that can shrug it off."""
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4}])
    )
    with pytest.raises(SonarrError):
        await SonarrClient(BASE_URL, "apikey").quality_profiles()


# --- TLS posture (shared with Radarr, so proven the same way) ---


def test_build_verify_is_radarrs_own_translation() -> None:
    """Imported, not reimplemented: one place to fix the CA-file escape hatch, and both
    services behave identically for a self-signed home server."""
    from app.services.radarr import build_verify as radarr_build_verify

    assert build_verify is radarr_build_verify
    assert build_verify(tls_verify=True, ca_file=None) is True
    assert build_verify(tls_verify=True, ca_file="/ca.pem") == "/ca.pem"
    assert build_verify(tls_verify=False, ca_file="/ca.pem") is False


class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        body = json.dumps({"appName": "Sonarr", "version": "tls-test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test-server logging
        pass


@pytest.fixture
def tls_sonarr(tmp_path: Path) -> Iterator[tuple[str, str]]:
    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(context)

    httpd = HTTPServer(("127.0.0.1", 0), _StatusHandler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_path))
    try:
        yield f"https://127.0.0.1:{port}", str(ca_path)
    finally:
        httpd.shutdown()


async def test_self_signed_rejected_by_default(tls_sonarr: tuple[str, str]) -> None:
    base_url, _ = tls_sonarr
    client = SonarrClient(base_url, "apikey", verify=True)
    with pytest.raises(SonarrConnectionError):
        await client.system_status()


async def test_self_signed_accepted_with_ca_file(tls_sonarr: tuple[str, str]) -> None:
    base_url, ca_path = tls_sonarr
    client = SonarrClient(base_url, "apikey", verify=ca_path)
    assert (await client.system_status())["version"] == "tls-test"


async def test_a_broken_ca_path_degrades_to_unreachable(tmp_path: Path) -> None:
    """A missing or misdirected CA file raises while BUILDING the context, before any
    socket — it must read as "unreachable", not 500 every page that touches Sonarr."""
    client = SonarrClient(BASE_URL, "apikey", verify=str(tmp_path / "nope.pem"))
    with pytest.raises(SonarrConnectionError):
        await client.system_status()


def test_monitor_options_are_sonarrs_own_spellings() -> None:
    """Passed through verbatim: "firstSeason" is camelCase because Sonarr's enum is, and
    a tidied "first_season" would be silently rejected by the server."""
    from app.services.sonarr import MONITOR_OPTIONS

    assert MONITOR_OPTIONS == (MONITOR_ALL, "future", MONITOR_FIRST_SEASON, "none")
    assert MONITOR_FIRST_SEASON == "firstSeason"
