"""Step 9 test: API behavior via respx + real TLS validation via trustme."""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import respx
import trustme

from app.services.radarr import (
    QUEUE_PAGE_SIZE,
    RadarrAuthError,
    RadarrClient,
    RadarrConnectionError,
    RadarrError,
    _image_from,
    build_verify,
)

BASE_URL = "http://radarr.local:7878"
API = f"{BASE_URL}/api/v3"


# --- API behavior (respx) ---


@respx.mock
async def test_system_status_returns_json() -> None:
    respx.get(f"{API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5.2.0"})
    )
    client = RadarrClient(BASE_URL, "apikey")
    assert (await client.system_status())["version"] == "5.2.0"


@respx.mock
async def test_list_movies_maps_fields() -> None:
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "hasFile": True},
                {"tmdbId": 872585, "title": "Oppenheimer", "year": 2023, "hasFile": False},
            ],
        )
    )
    movies = await RadarrClient(BASE_URL, "apikey").list_movies()
    assert movies[0].tmdb_id == 693134
    assert movies[0].has_file is True
    assert movies[1].has_file is False


@respx.mock
async def test_lookup_extracts_poster_and_genres() -> None:
    respx.get(f"{API}/movie/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tmdbId": 12345,
                    "title": "Neon Rain",
                    "year": 2025,
                    "overview": "A city that never sleeps.",
                    "genres": ["Action", "Sci-Fi"],
                    "images": [
                        {"coverType": "fanart", "remoteUrl": "http://x/fan.jpg"},
                        {"coverType": "poster", "remoteUrl": "http://x/poster.jpg"},
                    ],
                    "imdbId": "tt9999999",
                }
            ],
        )
    )
    results = await RadarrClient(BASE_URL, "apikey").lookup("Neon Rain")
    assert results[0].poster_url == "http://x/poster.jpg"
    assert results[0].genres == ("Action", "Sci-Fi")


@respx.mock
async def test_add_movie_posts_payload() -> None:
    route = respx.post(f"{API}/movie").mock(
        return_value=httpx.Response(201, json={"tmdbId": 12345, "title": "Neon Rain", "year": 2025})
    )
    added = await RadarrClient(BASE_URL, "apikey").add_movie(
        tmdb_id=12345,
        title="Neon Rain",
        year=2025,
        quality_profile_id=4,
        root_folder_path="/movies",
    )
    assert added.tmdb_id == 12345
    sent = json.loads(route.calls.last.request.content)
    assert sent["qualityProfileId"] == 4
    assert sent["addOptions"]["searchForMovie"] is True


@respx.mock
async def test_upgrade_ignores_failed_search_command() -> None:
    # The profile PUT is the real state change; a failed search kick must not raise.
    respx.get(f"{API}/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "qualityProfileId": 4, "monitored": True})
    )
    put = respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={"id": 7}))
    command = respx.post(f"{API}/command").mock(return_value=httpx.Response(500))
    await RadarrClient(BASE_URL, "apikey").upgrade_movie(7, 5)  # does not raise
    assert put.called and command.called
    assert json.loads(put.calls.last.request.content)["qualityProfileId"] == 5


@respx.mock
async def test_upgrade_propagates_profile_put_failure() -> None:
    # A failure of the meaningful state change (the PUT) must still surface.
    respx.get(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={"id": 7}))
    respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(500))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").upgrade_movie(7, 5)


@respx.mock
async def test_401_maps_to_auth_error() -> None:
    respx.get(f"{API}/system/status").mock(return_value=httpx.Response(401))
    with pytest.raises(RadarrAuthError):
        await RadarrClient(BASE_URL, "wrong-key").system_status()


@respx.mock
async def test_connect_failure_maps_to_connection_error() -> None:
    respx.get(f"{API}/system/status").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RadarrConnectionError):
        await RadarrClient(BASE_URL, "apikey").system_status()


def test_build_verify_translations() -> None:
    assert build_verify(tls_verify=True, ca_file=None) is True
    assert build_verify(tls_verify=True, ca_file="/ca.pem") == "/ca.pem"
    assert build_verify(tls_verify=False, ca_file=None) is False


async def test_bad_ca_path_degrades_to_connection_error() -> None:
    # verify pointing at a directory (e.g. an empty BM_TLS_CA_FILE that became ".")
    # must surface as RadarrConnectionError, not a bare IsADirectoryError/500.
    client = RadarrClient(BASE_URL, "apikey", verify="/")
    with pytest.raises(RadarrConnectionError):
        await client.system_status()


# --- Real TLS validation (trustme + a throwaway HTTPS server) ---


class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        body = json.dumps({"version": "tls-test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test-server logging
        pass


@pytest.fixture
def tls_radarr(tmp_path: Path) -> Iterator[tuple[str, str]]:
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


async def test_self_signed_rejected_by_default(tls_radarr: tuple[str, str]) -> None:
    base_url, _ = tls_radarr
    client = RadarrClient(base_url, "apikey", verify=True)
    with pytest.raises(RadarrConnectionError):
        await client.system_status()


async def test_self_signed_accepted_with_ca_file(tls_radarr: tuple[str, str]) -> None:
    base_url, ca_path = tls_radarr
    client = RadarrClient(base_url, "apikey", verify=ca_path)
    assert (await client.system_status())["version"] == "tls-test"


@respx.mock
async def test_movie_by_tmdb_returns_the_single_match() -> None:
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[
            {"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024,
             "hasFile": True, "id": 42, "qualityProfileId": 7}
        ])
    )
    movie = await RadarrClient(BASE_URL, "apikey").movie_by_tmdb(693134)
    assert movie is not None
    assert (movie.tmdb_id, movie.radarr_id, movie.has_file) == (693134, 42, True)
    assert movie.quality_profile_id == 7  # the credits call needs radarr_id; the UI the rest


@respx.mock
async def test_movie_by_tmdb_returns_none_when_radarr_does_not_have_it() -> None:
    # An empty list is a definite answer: "I looked, it is not here."
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    assert await RadarrClient(BASE_URL, "apikey").movie_by_tmdb(693134) is None


@respx.mock
async def test_movie_by_tmdb_raises_rather_than_reporting_absence() -> None:
    # A failure must never look like "not in the library" — that distinction is what
    # keeps the movie modal from lying about the user's own collection.
    respx.get(f"{API}/movie").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RadarrConnectionError):
        await RadarrClient(BASE_URL, "apikey").movie_by_tmdb(693134)


@pytest.mark.parametrize(
    ("images", "expected"),
    [
        ([], None),
        ([{"coverType": "poster", "remoteUrl": "http://r/p.jpg", "url": "/local.jpg"}],
         "http://r/p.jpg"),                                   # remoteUrl wins
        ([{"coverType": "poster", "url": "/local.jpg"}], "/local.jpg"),   # falls back
        ([{"coverType": "poster", "remoteUrl": None, "url": "/local.jpg"}], "/local.jpg"),
        ([{"coverType": "fanart", "remoteUrl": "http://r/b.jpg"}], None),  # wrong type
        ([{"coverType": "fanart", "remoteUrl": "http://r/b.jpg"},
          {"coverType": "poster", "remoteUrl": "http://r/p.jpg"}], "http://r/p.jpg"),
        ([{"coverType": "poster"}], None),
        ([{}], None),
    ],
)
def test_image_from_picks_the_requested_cover_type(
    images: list[dict], expected: str | None
) -> None:
    """Pins the behaviour that _poster_from_images used to duplicate.

    Every cover type — poster, fanart, headshot — now resolves through this one function,
    so a future fallback added here reaches all three instead of one.
    """
    assert _image_from(images, "poster") == expected


@respx.mock
async def test_movie_detail_parses_the_documented_object_shape() -> None:
    respx.get(f"{API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json={"tmdbId": 693134, "title": "Dune: Part Two"})
    )
    detail = await RadarrClient(BASE_URL, "apikey").movie_detail(693134)
    assert (detail.tmdb_id, detail.title) == (693134, "Dune: Part Two")


@respx.mock
async def test_movie_detail_accepts_a_single_element_list() -> None:
    # A version that wraps the answer in a list is handled rather than crashing on .get.
    respx.get(f"{API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 693134, "title": "Dune: Part Two"}])
    )
    detail = await RadarrClient(BASE_URL, "apikey").movie_detail(693134)
    assert detail.title == "Dune: Part Two"


@pytest.mark.parametrize("body", [[], "a string", 42, True])
@respx.mock
async def test_movie_detail_raises_radarr_error_on_any_other_shape(body: object) -> None:
    """Must be RadarrError specifically: the caller catches that and renders "details
    unavailable", while an AttributeError would escape as a 500 page.

    An empty list raises rather than becoming a blank record — "no film" is a failure to
    answer, and a card with no title claiming to be a film is worse than saying so.
    """
    respx.get(f"{API}/movie/lookup/tmdb").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").movie_detail(693134)


@respx.mock
async def test_movie_detail_raises_radarr_error_on_a_json_null() -> None:
    respx.get(f"{API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").movie_detail(693134)


@pytest.mark.parametrize("body", [b"", b"<html>gateway error</html>"])
@respx.mock
async def test_movie_detail_raises_radarr_error_on_a_non_json_body(body: bytes) -> None:
    """A 200 with an empty or HTML body — a proxy in front of Radarr, or a truncated
    response. JSONDecodeError is a ValueError, which the caller does not catch either."""
    respx.get(f"{API}/movie/lookup/tmdb").mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").movie_detail(693134)


# --- a 200 that isn't JSON (Step 1) ---

# What a reverse proxy or captive portal in front of Radarr actually answers with.
PROXY_LOGIN_PAGE = "<html><body><h1>Sign in to continue</h1></body></html>"


@respx.mock
@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/system/status", lambda client: client.system_status()),
        ("/movie", lambda client: client.list_movies()),
        ("/movie", lambda client: client.movie_by_tmdb(693134)),
        ("/movie/lookup", lambda client: client.lookup("Dune")),
        ("/movie/lookup/tmdb", lambda client: client.movie_detail(693134)),
        ("/credit", lambda client: client.credits(7)),
        ("/qualityprofile", lambda client: client.quality_profiles()),
        ("/rootfolder", lambda client: client.root_folders()),
    ],
)
async def test_a_non_json_200_is_a_radarr_error(path: str, call) -> None:  # noqa: ANN001
    """Every read endpoint, not just /movie/lookup/tmdb.

    JSONDecodeError is not a RadarrError, so it escapes every caller: the dashboard
    500s and a scheduled run dies with no report written.
    """
    respx.get(f"{API}{path}").mock(return_value=httpx.Response(200, text=PROXY_LOGIN_PAGE))
    with pytest.raises(RadarrError):
        await call(RadarrClient(BASE_URL, "apikey"))


@respx.mock
async def test_a_non_json_200_on_add_is_a_radarr_error() -> None:
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, text=PROXY_LOGIN_PAGE))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").add_movie(
            tmdb_id=1, title="X", year=2026, quality_profile_id=4, root_folder_path="/movies"
        )


@respx.mock
async def test_a_non_json_200_on_upgrade_is_a_radarr_error() -> None:
    respx.get(f"{API}/movie/7").mock(return_value=httpx.Response(200, text=PROXY_LOGIN_PAGE))
    put = respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").upgrade_movie(7, 5)
    assert not put.called  # nothing was written back to Radarr


@respx.mock
@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/movie", lambda client: client.list_movies()),
        ("/movie/lookup", lambda client: client.lookup("Dune")),
        ("/credit", lambda client: client.credits(7)),
        ("/qualityprofile", lambda client: client.quality_profiles()),
        ("/rootfolder", lambda client: client.root_folders()),
    ],
)
async def test_an_object_where_a_list_belongs_is_refused(path: str, call) -> None:  # noqa: ANN001
    """Iterating a dict yields its KEYS — a shape surprise would become wrong data
    (e.g. a library of zero movies read as "Radarr has nothing") rather than an error."""
    respx.get(f"{API}{path}").mock(
        return_value=httpx.Response(200, json={"error": "not authorised"})
    )
    with pytest.raises(RadarrError):
        await call(RadarrClient(BASE_URL, "apikey"))


@respx.mock
async def test_a_profile_without_a_name_is_refused() -> None:
    # These feed the Settings "Adds as" dropdown; a half-formed entry is unusable.
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4}])
    )
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").quality_profiles()


@respx.mock
async def test_a_json_list_where_the_movie_record_belongs_is_refused() -> None:
    """Valid JSON, wrong shape — the case `_json` alone cannot catch.

    upgrade_movie mutates this object and PUTs it straight back, so a list would
    either crash with TypeError or overwrite the Radarr record with a bad shape.
    """
    respx.get(f"{API}/movie/7").mock(return_value=httpx.Response(200, json=[]))
    put = respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").upgrade_movie(7, 5)
    assert not put.called


@respx.mock
async def test_a_json_list_from_add_movie_is_refused() -> None:
    respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json=[]))
    with pytest.raises(RadarrError):
        await RadarrClient(BASE_URL, "apikey").add_movie(
            tmdb_id=1, title="X", year=2026, quality_profile_id=4, root_folder_path="/movies"
        )


# --- the download queue (F13) ---


def _client() -> RadarrClient:
    return RadarrClient(BASE_URL, "apikey")


@respx.mock
async def test_the_queue_reports_progress_per_movie() -> None:
    """580MB left of 1GB is 42% done. The client returns the percentage rather than the
    byte counts: every caller wants the same derived figure, and deriving it twice is how
    two pages come to disagree."""
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"movieId": 7, "size": 1_000_000_000, "sizeleft": 580_000_000},
        {"movieId": 9, "size": 2_000_000_000, "sizeleft": 0},
    ]}))

    progress = await _client().queue()

    assert round(progress[7]) == 42
    assert progress[9] == 100.0


@respx.mock
async def test_a_record_radarr_has_not_sized_is_zero_not_a_crash() -> None:
    """Radarr reports size 0 between grabbing a release and learning how big it is."""
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"movieId": 7, "size": 0, "sizeleft": 0},
    ]}))

    assert await _client().queue() == {7: 0.0}


@respx.mock
async def test_progress_is_clamped_to_the_possible() -> None:
    """Radarr briefly reports sizeleft above size while it revises an estimate, and a
    negative percentage would draw a negative fill."""
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"movieId": 7, "size": 100, "sizeleft": 150},
        {"movieId": 8, "size": 100, "sizeleft": -10},
    ]}))

    progress = await _client().queue()

    assert progress[7] == 0.0
    assert progress[8] == 100.0


@respx.mock
async def test_two_records_for_one_film_take_the_slower() -> None:
    """A film is no nearer than its slowest part."""
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"movieId": 7, "size": 100, "sizeleft": 70},   # 30% — listed FIRST, so a
        {"movieId": 7, "size": 100, "sizeleft": 10},   # 90%   last-wins fold would say 90
    ]}))

    assert round((await _client().queue())[7]) == 30


@respx.mock
async def test_unusable_queue_records_are_skipped_not_guessed() -> None:
    """The queue is scraped-shaped data like any other Radarr answer: a record missing its
    ids is dropped, and the rest of the page still gets its progress."""
    respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"size": 100, "sizeleft": 50},                      # no movieId
        {"movieId": "7", "size": 100, "sizeleft": 50},       # id as a string
        {"movieId": 8, "sizeleft": 50},                      # no size
        "not a record",
        {"movieId": 9, "size": 100, "sizeleft": 25},         # the only usable one
    ]}))

    assert await _client().queue() == {9: 75.0}


@respx.mock
async def test_a_queue_that_is_not_a_queue_raises_rather_than_returning_nothing() -> None:
    """A proxy can answer this path with anything. An empty dict would mean "nothing is
    downloading", which is a different claim from "we could not look" — so the shape
    surprise is raised, exactly as the list endpoints raise theirs."""
    for body in ([], {"items": []}, {"records": "no"}):
        respx.get(f"{API}/queue").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(RadarrError):
            await _client().queue()


@respx.mock
async def test_the_queue_is_asked_for_one_bounded_page() -> None:
    """An unbounded page is a request whose size Radarr decides."""
    route = respx.get(f"{API}/queue").mock(
        return_value=httpx.Response(200, json={"records": []})
    )

    await _client().queue()

    assert route.calls.last.request.url.params["pageSize"] == str(QUEUE_PAGE_SIZE)
