"""F22: the movie detail route, its fragment, and the poster links that open it."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
    imdb_url,
)
from app.web.deps import RADARR_LIBRARY_TIMEOUT_SECONDS, safe_external_url
from app.web.movies import (
    CAST_UNAVAILABLE_MESSAGE,
    DETAIL_TIMEOUT_SECONDS,
    NO_CAST_MESSAGE,
    UNAVAILABLE_MESSAGE,
    _minutes_display,
    _pick_crew,
)
from tests.conftest import AppHarness
from tests.integration.conftest import QUEUE_ROUTE, queue_records

RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
API = f"{RADARR_URL}/api/v3"
TMDB_ID = 693134
RADARR_ID = 42

LOOKUP = {
    "tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
    "overview": "Paul Atreides unites with the Fremen.",
    "runtime": 167, "certification": "PG-13", "studio": "Legendary Pictures",
    "originalLanguage": {"id": 1, "name": "English"}, "status": "released",
    "website": "https://example.test/dune", "youTubeTrailerId": "U2Qp5pL3ovA",
    "genres": ["Science Fiction", "Adventure"], "imdbId": "tt15239678",
    "images": [{"coverType": "poster",
                "remoteUrl": "https://image.tmdb.org/t/p/original/p.jpg"},
               {"coverType": "fanart",
                "remoteUrl": "https://image.tmdb.org/t/p/original/b.jpg"}],
    "ratings": {"imdb": {"value": 8.4}, "tmdb": {"value": 8.131},
                "rottenTomatoes": {"value": 92}, "metacritic": {"value": 79}},
}
CREDITS = [
    {"personName": "Timothee Chalamet", "character": "Paul", "type": "cast", "order": 0,
     "images": [{"coverType": "headshot",
                "remoteUrl": "https://image.tmdb.org/t/p/original/h1.jpg"}]},
    {"personName": "Zendaya", "character": "Chani", "type": "cast", "order": 1, "images": []},
    {"personName": "Denis Villeneuve", "job": "Director", "type": "crew", "images": []},
    {"personName": "Someone Else", "job": "Executive Producer", "type": "crew", "images": []},
]


def _add_app(harness: AppHarness) -> None:
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)


def _mock_lookup() -> None:
    respx.get(f"{API}/movie/lookup/tmdb").mock(return_value=httpx.Response(200, json=LOOKUP))


def _mock_library(movies: list[dict]) -> None:
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=movies))


def _mock_images() -> None:
    respx.get(url__regex=r"https://image\.tmdb\.org/.*").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg")
    )


def test_minutes_display() -> None:
    assert _minutes_display(167) == "2h 47m"
    assert _minutes_display(120) == "2h"
    assert _minutes_display(45) == "45m"
    assert _minutes_display(None) is None
    assert _minutes_display(0) is None


def test_pick_crew_prefers_heads_of_department() -> None:
    from app.services.radarr import CreditPerson

    crew = (
        CreditPerson("Producer Person", "Producer", None),
        CreditPerson("Denis Villeneuve", "Director", None),
        CreditPerson("Jon Spaihts", "Writer", None),
    )
    assert [person.role for person in _pick_crew(crew)] == ["Director", "Writer"]


@respx.mock
async def test_detail_page_shows_the_full_record(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "Dune: Part Two" in page
    assert "Paul Atreides unites with the Fremen." in page  # overview
    assert "Science Fiction" in page and "Adventure" in page  # genres
    assert "2h 47m" in page and "PG-13" in page and "Legendary Pictures" in page
    for source in ("IMDb", "TMDB", "Rotten Tomatoes", "Metacritic"):
        assert source in page
    assert "8.4" in page and "92%" in page


@respx.mock
async def test_a_title_not_in_the_library_explains_the_missing_cast(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert NO_CAST_MESSAGE in page
    assert "Timothee Chalamet" not in page


@respx.mock
async def test_a_library_title_shows_cast_and_crew(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "Timothee Chalamet" in page and "Paul" in page
    assert "Zendaya" in page
    assert "Denis Villeneuve" in page and "Director" in page
    assert "Someone Else" not in page  # Executive Producer is not a head-of-department role
    assert NO_CAST_MESSAGE not in page


@respx.mock
async def test_fragment_omits_the_page_chrome(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text
    assert "Dune: Part Two" in fragment
    assert "<!DOCTYPE html>" not in fragment  # no layout — it drops into the dialog
    assert "topnav" not in fragment


@respx.mock
async def test_headshots_are_served_from_this_origin(harness: AppHarness) -> None:
    # CSP is img-src 'self': a remote TMDB URL in the markup would be blocked.
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "image.tmdb.org" not in page
    assert "/posters/" in page


@respx.mock
async def test_unreachable_radarr_explains_itself(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    # Dead means dead: the library lookup the route now makes first fails too.
    respx.get(f"{API}/movie/lookup/tmdb").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{API}/movie").mock(side_effect=httpx.ConnectError("down"))

    page = harness.client.get(f"/movies/{TMDB_ID}")
    assert page.status_code == 200  # a dead Radarr is a message, not a 500
    assert UNAVAILABLE_MESSAGE in page.text


async def test_detail_requires_a_session(harness: AppHarness) -> None:
    harness.client.cookies.clear()
    response = harness.client.get(f"/movies/{TMDB_ID}", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def _seed_report(harness: AppHarness, tmdb_id: int | None) -> None:
    movie = MovieResult(
        rank=1, title="Dune: Part Two", normalized_title="dune part two",
        gross_amount=45_000_000, gross_display="$45.0M", weeks_in_release=1,
        status=MovieStatus.WANTED, action=MovieAction.NONE, tmdb_id=tmdb_id,
    )
    harness.client.app.state.reports.save(Report(
        id="report-20260814-100000-aaaa", run_at="2026-08-14T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W27",
        totals=ReportTotals(movies=1, matched=1), movies=[movie],
    ))


def test_report_posters_link_to_the_detail(harness: AppHarness) -> None:
    harness.activate()
    _seed_report(harness, TMDB_ID)
    page = harness.client.get("/reports/report-20260814-100000-aaaa").text
    assert f'href="/movies/{TMDB_ID}"' in page
    assert f'data-movie="{TMDB_ID}"' in page


def test_an_unmatched_title_has_no_dead_link(harness: AppHarness) -> None:
    # Radarr never identified it, so there is no id to open — the frame must not be a link.
    harness.activate()
    _seed_report(harness, None)
    page = harness.client.get("/reports/report-20260814-100000-aaaa").text
    assert "data-movie=" not in page
    assert "poster-frame-plain" in page


def test_every_page_carries_the_dialog(harness: AppHarness) -> None:
    harness.activate()
    for path in ("/dashboard", "/reports", "/settings"):
        assert 'id="movie-dialog"' in harness.client.get(path).text, path


def test_the_login_page_has_no_dialog(harness: AppHarness) -> None:
    assert 'id="movie-dialog"' not in harness.client.get("/login").text


@respx.mock
async def test_images_are_requested_at_display_size_not_original(harness: AppHarness) -> None:
    """A 2000px poster into a 208px box is a 9.6x downscale, which a GPU rasterizer
    renders with plain bilinear filtering — visibly aliased lettering. Ask TMDB for a
    size close to the box instead."""
    from app.services.posters import HEADSHOT_WIDTH, POSTER_WIDTH

    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    poster = respx.get(f"https://image.tmdb.org/t/p/{POSTER_WIDTH}/p.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg"))
    headshot = respx.get(f"https://image.tmdb.org/t/p/{HEADSHOT_WIDTH}/h1.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg"))

    harness.client.get(f"/movies/{TMDB_ID}")
    assert poster.called, "the poster was not requested at the display size"
    assert headshot.called, "the headshot was not requested at the display size"


@respx.mock
async def test_an_unreadable_library_never_claims_the_film_is_missing(
    harness: AppHarness,
) -> None:
    """The detail lookup gets 8s, the whole-library fetch 4s. A Radarr that answers one
    and not the other must not be reported as "you don't own this film"."""
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    respx.get(f"{API}/movie").mock(side_effect=httpx.ConnectError("library unreachable"))
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "Dune: Part Two" in page  # the record still renders
    assert NO_CAST_MESSAGE not in page  # never blames the library we could not read
    assert CAST_UNAVAILABLE_MESSAGE in page  # says what actually happened


@respx.mock
async def test_a_library_film_whose_credits_fail_says_so(harness: AppHarness) -> None:
    # In the library, but /credit errored — also not the user's fault, also not silence.
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    respx.get(f"{API}/credit").mock(side_effect=httpx.ConnectError("credits down"))
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "Dune: Part Two" in page
    assert NO_CAST_MESSAGE not in page
    assert CAST_UNAVAILABLE_MESSAGE in page


@respx.mock
async def test_a_library_film_with_credits_says_nothing(harness: AppHarness) -> None:
    # The happy path must stay silent — no explanatory note when there is nothing to explain.
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "Timothee Chalamet" in page
    assert NO_CAST_MESSAGE not in page
    assert CAST_UNAVAILABLE_MESSAGE not in page


@respx.mock
async def test_the_library_check_asks_for_one_film_not_the_whole_library(
    harness: AppHarness,
) -> None:
    """Pins that the tmdbId filter is actually sent.

    Verified against a live Radarr before adoption — the filter is honoured there — but a
    refactor that dropped the parameter would silently fall back to the whole library and
    take matches[0], i.e. an arbitrary film reported as the user's.
    """
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    library = respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": TMDB_ID, "title": "Dune: Part Two",
                                                "year": 2024, "hasFile": True, "id": RADARR_ID}])
    )
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    _mock_images()

    harness.client.get(f"/movies/{TMDB_ID}")

    assert library.called
    assert library.calls.last.request.url.params["tmdbId"] == str(TMDB_ID)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/dune",
        "http://example.test",
        "HTTPS://Example.TEST",       # schemes are case-insensitive
        "  https://example.test  ",   # a stray space must not drop a real link
        "https://fine.test/a?b=c#d",
    ],
)
def test_safe_external_url_keeps_real_links(url: str) -> None:
    assert safe_external_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//evil.example",             # scheme-relative, inherits the page's scheme
        "/relative",
        "\tjavascript:alert(1)",
        "http://ok\njavascript:alert(1)",   # control chars: browsers strip, we refuse
        "http://ok\tx",
        "",
        None,
    ],
)
def test_safe_external_url_drops_everything_else(url: str | None) -> None:
    assert safe_external_url(url) is None


@respx.mock
async def test_a_hostile_website_value_is_not_rendered_as_a_link(
    harness: AppHarness,
) -> None:
    """Radarr passes `website` through verbatim, so it is the one href here whose scheme
    upstream data controls. CSP would stop it executing; this stops it being a link."""
    harness.activate()
    _add_app(harness)
    hostile = dict(LOOKUP, website="javascript:alert(1)")
    respx.get(f"{API}/movie/lookup/tmdb").mock(return_value=httpx.Response(200, json=hostile))
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "javascript:" not in page
    assert "Official site" not in page  # the link is dropped, not rendered empty
    assert "Dune: Part Two" in page     # everything else still renders


@respx.mock
async def test_a_normal_website_value_still_links(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _mock_lookup()  # LOOKUP carries website="https://example.test/dune"
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert 'href="https://example.test/dune"' in page
    assert "Official site" in page


@pytest.mark.parametrize("body", [[], "unexpected", 42])
@respx.mock
async def test_a_surprising_lookup_shape_renders_a_message_not_a_500(
    harness: AppHarness, body: object
) -> None:
    """The guard only pays off if it lands on the handled path: movies.py catches
    RadarrError but not AttributeError, so a shape change must raise the former."""
    harness.activate()
    _add_app(harness)
    _mock_library([])  # the library answers normally; only the detail shape is odd
    respx.get(f"{API}/movie/lookup/tmdb").mock(return_value=httpx.Response(200, json=body))

    page = harness.client.get(f"/movies/{TMDB_ID}")
    assert page.status_code == 200
    assert UNAVAILABLE_MESSAGE in page.text


# --- the modal's IMDb link comes from the shared builder (review step 10) ---


@respx.mock
def test_the_modal_renders_the_imdb_link(harness: AppHarness) -> None:
    """The refactor to `reports.imdb_url` had nothing covering it: the pipeline and the
    match fixer both assert their IMDb URLs, the modal never did."""
    harness.activate()
    _add_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert f'href="{imdb_url(LOOKUP["imdbId"])}"' in page
    assert "https://www.imdb.com/title/tt15239678/" in page  # the built value, spelled out


@respx.mock
def test_a_film_with_no_imdb_id_renders_no_imdb_link(harness: AppHarness) -> None:
    # The helper returns None for a missing id, and the template drops the anchor.
    harness.activate()
    _add_app(harness)
    respx.get(f"{API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json={**LOOKUP, "imdbId": None})
    )
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert "imdb.com/title" not in page
    assert "Dune: Part Two" in page  # the rest of the record still renders


# --- credits come from whichever connection holds the film, not the first one added ---

SECOND_URL = "http://radarr-main.local:7878"
SECOND_KEY = "fedcba9876543210fedcba9876543210"  # noqa: S105 — the suite's dummy key
SECOND_API = f"{SECOND_URL}/api/v3"
SECOND_RADARR_ID = 150  # deliberately not RADARR_ID: an id is meaningless off its own box


def _add_second_app(harness: AppHarness) -> None:
    """A second connection, added after the primary and never made primary."""
    harness.client.app.state.apps.add(name="Main", url=SECOND_URL, api_key=SECOND_KEY)


def _library_entry(radarr_id: int) -> dict:
    return {"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
            "hasFile": True, "id": radarr_id, "imdbId": "tt15239678"}


@respx.mock
async def test_cast_loads_for_a_film_held_only_by_a_secondary_connection(
    harness: AppHarness,
) -> None:
    """The reported bug: with two connections, a film added to the second showed
    "add this one and the full credits appear here" — about a film already added."""
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    _mock_lookup()
    _mock_library([])  # the primary does NOT have it
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(SECOND_RADARR_ID)])
    )
    respx.get(f"{SECOND_API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json=LOOKUP)
    )
    credits_route = respx.get(f"{SECOND_API}/credit").mock(
        return_value=httpx.Response(200, json=CREDITS)
    )

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert "Timothee Chalamet" in page
    assert "Denis Villeneuve" in page
    assert NO_CAST_MESSAGE not in page
    assert CAST_UNAVAILABLE_MESSAGE not in page
    assert credits_route.called


@respx.mock
async def test_the_credits_call_uses_the_holder_own_radarr_id(harness: AppHarness) -> None:
    """A Radarr id is issued by one box and means nothing on another: asking the primary
    for movieId=150 would return someone else's film, or nothing."""
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    _mock_lookup()
    _mock_library([])
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(SECOND_RADARR_ID)])
    )
    respx.get(f"{SECOND_API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json=LOOKUP)
    )
    second_credits = respx.get(f"{SECOND_API}/credit").mock(
        return_value=httpx.Response(200, json=CREDITS)
    )
    primary_credits = respx.get(f"{API}/credit").mock(
        return_value=httpx.Response(200, json=[])
    )

    harness.client.get(f"/movies/{TMDB_ID}")

    assert not primary_credits.called  # the box that does not hold it is never asked
    assert second_credits.calls.last.request.url.params["movieId"] == str(SECOND_RADARR_ID)


@respx.mock
async def test_a_film_on_no_connection_still_says_so(harness: AppHarness) -> None:
    # The honest case must survive: every box answered, none has it.
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    _mock_lookup()
    _mock_library([])
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert NO_CAST_MESSAGE in page


@respx.mock
async def test_one_silent_connection_never_claims_the_film_is_missing(
    harness: AppHarness,
) -> None:
    """The primary says no and the other box says nothing. The film may well be sitting
    on it, so the message must not blame the user's library."""
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    _mock_lookup()
    _mock_library([])
    respx.get(f"{SECOND_API}/movie").mock(side_effect=httpx.ConnectError("down"))

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert "Dune: Part Two" in page          # the record still renders
    assert CAST_UNAVAILABLE_MESSAGE in page
    assert NO_CAST_MESSAGE not in page


@respx.mock
async def test_a_down_primary_no_longer_blanks_a_film_another_box_holds(
    harness: AppHarness,
) -> None:
    """Metadata used to come from the primary alone, so one dead box hid a film that a
    live connection could describe in full."""
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    respx.get(f"{API}/movie").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{API}/movie/lookup/tmdb").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(SECOND_RADARR_ID)])
    )
    respx.get(f"{SECOND_API}/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json=LOOKUP)
    )
    respx.get(f"{SECOND_API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert UNAVAILABLE_MESSAGE not in page
    assert "Dune: Part Two" in page
    assert "Timothee Chalamet" in page


@respx.mock
async def test_a_film_on_both_connections_reads_from_one_of_them(
    harness: AppHarness,
) -> None:
    # Duplicated across boxes is normal (HD on one, 4K on the other) and must not double
    # up the cast or pick the box that cannot answer.
    harness.activate()
    _add_app(harness)
    _add_second_app(harness)
    _mock_images()
    _mock_lookup()
    _mock_library([_library_entry(RADARR_ID)])
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(SECOND_RADARR_ID)])
    )
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    respx.get(f"{SECOND_API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert page.count("Timothee Chalamet") == 1
    assert CAST_UNAVAILABLE_MESSAGE not in page


def test_the_per_connection_lookup_is_bounded_tighter_than_the_metadata_call() -> None:
    """Every modal open now waits on every connection, where it used to talk to one. A
    box that is down must not hold the dialog for the longer metadata budget, so the
    lookup takes the same bound the dashboard allows for reading a library."""
    assert RADARR_LIBRARY_TIMEOUT_SECONDS < DETAIL_TIMEOUT_SECONDS


# --- deciding and acting in one place (F10) ---


def _configured_app(harness: AppHarness, name: str = "Main", url: str = RADARR_URL) -> str:
    """A connection with a quality and folder of its own — what Add needs to be usable."""
    app = harness.client.app.state.apps.add(name=name, url=url, api_key=RADARR_KEY)
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=4, root_folder="/movies"
    )
    return app.id


@respx.mock
async def test_the_modal_offers_to_add_a_film_it_has_just_described(
    harness: AppHarness,
) -> None:
    """The whole point: the modal is the page built for deciding — ratings, genres, cast,
    overview — and used to make you close it and hunt for the card to act."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert 'action="/add-movie"' in fragment
    assert 'name="csrf_token"' in fragment
    assert 'name="tmdb_id" value="693134"' in fragment
    # No week to return to — the add sends the user back to this film instead.
    assert 'name="report_id" value=""' in fragment


@respx.mock
async def test_a_film_already_held_says_where_instead_of_offering_a_duplicate(
    harness: AppHarness,
) -> None:
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    _mock_images()
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert "In Library ·" in fragment and ">Main</a>" in fragment
    assert 'action="/add-movie"' not in fragment


@respx.mock
async def test_a_film_queued_on_a_box_reads_wanted_not_in_library(
    harness: AppHarness,
) -> None:
    """Same distinction the dashboard chips make — an added film with no file yet is on
    its way, not there."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": False, "id": RADARR_ID}])
    _mock_images()
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert "Wanted ·" in fragment and ">Main</a>" in fragment


@respx.mock
async def test_the_full_page_carries_the_same_control_as_the_modal(
    harness: AppHarness,
) -> None:
    """One template serves both, so a JS-off user gets the action too."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert 'action="/add-movie"' in page
    assert "<!DOCTYPE html>" in page


@respx.mock
async def test_adding_from_the_modal_lands_the_film_and_returns_to_it(
    harness: AppHarness,
) -> None:
    """The add route is the existing one, so this proves the new entry point reaches it —
    and that a film with no report behind it returns somewhere that explains the outcome.
    """
    harness.activate()
    _configured_app(harness)
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    _mock_library([])
    added = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={"id": 7}))

    response = harness.client.post(
        "/add-movie",
        data={"report_id": "", "tmdb_id": str(TMDB_ID), "title": "Dune: Part Two",
              "year": "2024"},
        follow_redirects=False,
    )

    assert added.called
    assert response.headers["location"] == f"/movies/{TMDB_ID}?status=added"


@respx.mock
async def test_an_add_that_fails_still_explains_itself_on_the_film(
    harness: AppHarness,
) -> None:
    """The reason this cannot simply keep returning to the reports list: that page renders
    no add outcome at all, so a rejected add from the modal would have been silent."""
    harness.activate()
    _configured_app(harness)
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    _mock_library([])
    respx.post(f"{API}/movie").mock(side_effect=httpx.ConnectError("down"))
    _mock_lookup()
    _mock_images()

    response = harness.client.post(
        "/add-movie",
        data={"report_id": "", "tmdb_id": str(TMDB_ID), "title": "Dune: Part Two"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/movies/{TMDB_ID}?status=add_failed"

    page = harness.client.get(response.headers["location"]).text
    assert "Radarr rejected the request" in page


@respx.mock
async def test_an_add_from_a_week_still_returns_to_that_week(harness: AppHarness) -> None:
    """The existing path is untouched — a report id still wins."""
    harness.activate()
    _configured_app(harness)
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    _mock_library([])
    respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={"id": 7}))

    response = harness.client.post(
        "/add-movie",
        data={"report_id": "report-abc", "tmdb_id": str(TMDB_ID), "title": "Dune: Part Two"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/reports/report-abc?status=added"


@respx.mock
async def test_opening_the_modal_asks_radarr_for_nothing_extra(harness: AppHarness) -> None:
    """The Add menu reads cached options rather than fetching them. The modal is the
    fast-open path, and `add_movie` re-resolves the quality and folder live before it
    sends anything — so a live fetch here would buy latency and no safety."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([])
    _mock_images()
    profiles = respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    folders = respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert 'action="/add-movie"' in fragment  # the control is there all the same
    assert not profiles.called
    assert not folders.called


@respx.mock
async def test_the_held_name_opens_that_film_on_that_radarr(harness: AppHarness) -> None:
    """Naming the box was a dead end. For the jobs BoxMedia deliberately leaves to Radarr
    — the queue, the files, the history — the name is now the way there."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID, "titleSlug": "dune-part-two-693134"}])
    _mock_images()
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert f'href="{RADARR_URL}/movie/dune-part-two-693134"' in fragment
    assert 'rel="noopener noreferrer"' in fragment


@respx.mock
async def test_a_held_name_without_a_route_stays_plain_text(harness: AppHarness) -> None:
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": True, "id": RADARR_ID}])
    _mock_images()
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert ">Main</a>" in fragment
    assert f"{RADARR_URL}/movie/" not in fragment


# --- progress everywhere, and kept current (F13 follow-up) ---


def _downloading(harness: AppHarness, *, radarr_id: int = 42, sizeleft: int = 580_000_000):
    """The film on the primary, still on its way, 42% of a 1GB release done."""
    _mock_library([{"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024,
                    "hasFile": False, "id": radarr_id, "titleSlug": "dune-part-two-693134"}])
    # The film is in the library (still downloading), so the modal asks for its credits.
    respx.get(f"{API}/credit").mock(return_value=httpx.Response(200, json=CREDITS))
    queue_records([{"movieId": radarr_id, "size": 1_000_000_000, "sizeleft": sizeleft}])


@respx.mock
async def test_the_movie_modal_shows_how_far_along_a_download_is(
    harness: AppHarness,
) -> None:
    """It was left out when this shipped, to keep the modal cheap to open. It is the page
    opened from the dashboard, from a weekly card and from the month leaderboard alike —
    so "everywhere" has to include it."""
    harness.activate()
    _configured_app(harness)
    _mock_lookup()
    _mock_images()
    _downloading(harness)

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert "Wanted ·" in fragment
    assert ">42</span>%" in fragment


@respx.mock
async def test_the_modals_progress_is_keyed_for_the_poller(harness: AppHarness) -> None:
    """A download belongs to one connection AND one Radarr id — an id means nothing on a
    box that did not issue it, so both are in the key."""
    harness.activate()
    app_id = _configured_app(harness)
    _mock_lookup()
    _mock_images()
    _downloading(harness, radarr_id=77)

    fragment = harness.client.get(f"/movies/{TMDB_ID}?fragment=1").text

    assert f'data-progress="{app_id}:77"' in fragment


@respx.mock
async def test_progress_reports_what_every_connection_is_downloading(
    harness: AppHarness,
) -> None:
    """The one thing a rendered page cannot do for itself: 40% is not 40% a minute later."""
    harness.activate()
    app_id = _configured_app(harness)
    queue_records([
        {"movieId": 7, "size": 1_000_000_000, "sizeleft": 580_000_000},
        {"movieId": 9, "size": 100, "sizeleft": 0},
    ])

    response = harness.client.get("/progress")

    assert response.status_code == 200
    body = response.json()
    assert round(body[f"{app_id}:7"]) == 42
    assert body[f"{app_id}:9"] == 100.0
    # A percentage is stale the moment it is cached.
    assert "no-store" in response.headers["cache-control"]


@respx.mock
async def test_progress_needs_a_session(harness: AppHarness) -> None:
    """Behind the same gate as every other route — it is read-only, but it is still a
    live look at what this install is doing."""
    response = harness.client.get("/progress", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


@respx.mock
async def test_a_connection_that_cannot_be_read_contributes_nothing(
    harness: AppHarness,
) -> None:
    """Not zeroes. A Radarr that went away must leave the last-known figures on screen
    rather than resetting every bar to "just started"."""
    harness.activate()
    _configured_app(harness)
    respx.routes[QUEUE_ROUTE].mock(side_effect=httpx.ConnectError("gone"))

    assert harness.client.get("/progress").json() == {}


def test_the_page_tells_the_script_where_to_ask(harness: AppHarness) -> None:
    """app.js builds no URLs — it has no idea what url_base is — so the endpoint comes off
    an element, the same way every other address it uses does."""
    # Signed OUT first: the endpoint is behind the session gate, so offering it to a login
    # screen would only produce a redirect the poller cannot use.
    assert "data-progress-url" not in harness.client.get("/login").text

    harness.activate()
    assert 'data-progress-url="/progress"' in harness.client.get("/dashboard").text
