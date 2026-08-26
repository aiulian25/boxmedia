"""TV step 7 test: the Trakt contract via respx.

Small, like the client. The assertions that carry weight are the four ids — which are
the whole reason to rank by Trakt rather than by TMDB's own popularity — and the typed
auth error the Settings Test button names.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.discovery import TRAKT_BASE_URL
from app.services.trakt import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    TraktAuthError,
    TraktClient,
    TraktConnectionError,
    TraktError,
)

API = TRAKT_BASE_URL
CLIENT_ID = "Zx-9QwErTyUiOpAsDfGhJkLzXcVbNm1234567890abc"

TRENDING_ROW = {
    "watchers": 1842,
    "show": {
        "title": "The Hollow Coast",
        "year": 2023,
        "overview": "A survey team on a fault line that should not exist.",
        "ids": {
            "trakt": 9001,
            "slug": "the-hollow-coast",
            "imdb": "tt0944947",
            "tmdb": 1396,
            "tvdb": 121361,
        },
    },
}

ANTICIPATED_ROW = {
    "list_count": 5127,
    "show": {
        "title": "Ash Cartography",
        "year": 2026,
        "ids": {"trakt": 9002, "slug": "ash-cartography", "tmdb": 4242, "tvdb": 424242},
    },
}


def _client() -> TraktClient:
    return TraktClient(CLIENT_ID)


# --- the four ids: why Trakt ranks Discover at all ---


@respx.mock
async def test_a_trending_row_carries_all_four_ids() -> None:
    """The point of this module. A Trakt row can go to Sonarr immediately; a TMDB row
    has to be bridged through external_ids first."""
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[TRENDING_ROW])
    )
    show = (await _client().trending_shows())[0]

    assert show.trakt_id == 9001
    assert show.slug == "the-hollow-coast"
    assert show.imdb_id == "tt0944947"
    assert show.tmdb_id == 1396
    assert show.tvdb_id == 121361
    # Sonarr keys on TVDB, so this row needs no bridging call.
    assert show.addable is True


@respx.mock
async def test_a_row_without_a_tvdb_id_is_not_addable_without_bridging() -> None:
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[{
            **TRENDING_ROW,
            "show": {**TRENDING_ROW["show"], "ids": {"trakt": 1, "tmdb": 5}},
        }])
    )
    show = (await _client().trending_shows())[0]

    assert show.tvdb_id is None
    assert show.addable is False
    # But the TMDB id survived, which is what the bridge needs.
    assert show.tmdb_id == 5


@respx.mock
async def test_trending_maps_title_year_and_watchers() -> None:
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[TRENDING_ROW])
    )
    show = (await _client().trending_shows())[0]

    assert show.title == "The Hollow Coast"
    assert show.year == 2023
    assert show.watchers == 1842
    assert show.overview.startswith("A survey team")
    # Not the same measurement, so not the same field.
    assert show.list_count is None


@respx.mock
async def test_anticipated_maps_its_own_count() -> None:
    """`list_count` is how many people are WAITING; `watchers` is how many are watching.
    Collapsing them into one score would make a row claim a number that means whatever
    the caller assumed."""
    respx.get(f"{API}/shows/anticipated").mock(
        return_value=httpx.Response(200, json=[ANTICIPATED_ROW])
    )
    show = (await _client().anticipated_shows())[0]

    assert show.title == "Ash Cartography"
    assert show.list_count == 5127
    assert show.watchers is None
    # Anticipated rows routinely carry no overview and no imdb id yet.
    assert show.overview is None
    assert show.imdb_id is None


@respx.mock
async def test_trakt_order_is_preserved() -> None:
    """Trakt returns them ranked. Re-sorting locally would silently disagree with the
    service the row claims to be quoting."""
    second = {
        "watchers": 12,
        "show": {"title": "Quieter Show", "year": 2024, "ids": {"trakt": 2, "tvdb": 3}},
    }
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[TRENDING_ROW, second])
    )
    shows = await _client().trending_shows()
    assert [show.title for show in shows] == ["The Hollow Coast", "Quieter Show"]


# --- request shape ---


@respx.mock
async def test_the_client_id_travels_in_a_header_and_never_in_the_url() -> None:
    """Unlike TMDB's key. It is why nothing on the Trakt side needs redaction."""
    route = respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _client().trending_shows()

    request = route.calls.last.request
    assert request.headers["trakt-api-key"] == CLIENT_ID
    assert request.headers["trakt-api-version"] == "2"
    assert CLIENT_ID not in str(request.url)


@respx.mock
async def test_the_user_agent_is_ours_and_names_the_running_build() -> None:
    """The teardown found nzb360 announcing itself as "nzb360/1.0" to Trakt. Borrowing
    another application's identity is both rude and useless to whoever is trying to work
    out where their traffic comes from."""
    from app import __version__

    route = respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _client().trending_shows()

    agent = route.calls.last.request.headers["User-Agent"]
    assert agent.startswith("BoxMedia/")
    assert __version__ in agent
    assert "nzb360" not in agent


@respx.mock
async def test_the_headers_come_from_the_same_builder_the_probe_uses() -> None:
    """One builder, so the Settings Test button and the real client cannot disagree
    about what a Trakt request looks like."""
    from app.services.discovery import trakt_headers

    route = respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _client().trending_shows()

    sent = route.calls.last.request.headers
    for name, value in trakt_headers(CLIENT_ID).items():
        assert sent[name] == value


@respx.mock
async def test_trending_asks_for_the_full_payload_and_anticipated_does_not() -> None:
    """`extended=full` brings the overview; an anticipated show often has none written
    yet, so the extra bytes would buy nothing."""
    trending = respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[])
    )
    anticipated = respx.get(f"{API}/shows/anticipated").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _client().trending_shows()
    await _client().anticipated_shows()

    assert trending.calls.last.request.url.params["extended"] == "full"
    assert "extended" not in anticipated.calls.last.request.url.params


@respx.mock
async def test_the_limit_is_bounded_here_where_it_can_be_seen() -> None:
    """Trakt silently ignores an over-large limit rather than erroring, so a caller
    asking for 5000 would quietly get a default page and never know."""
    route = respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[])
    )
    await _client().trending_shows(limit=5000)
    assert route.calls.last.request.url.params["limit"] == str(MAX_LIMIT)

    await _client().trending_shows(limit=0)
    assert route.calls.last.request.url.params["limit"] == "1"

    await _client().trending_shows()
    assert route.calls.last.request.url.params["limit"] == str(DEFAULT_LIMIT)


# --- robustness ---


@respx.mock
async def test_a_malformed_row_is_skipped_not_fatal() -> None:
    """One bad row must not empty a Discover shelf."""
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[
            {"watchers": 1},                       # no show at all
            {"watchers": 2, "show": "not-a-dict"},
            {"watchers": 3, "show": {"ids": {"trakt": 1}}},  # no title to render
            "not-a-row",
            TRENDING_ROW,
        ])
    )
    shows = await _client().trending_shows()
    assert [show.title for show in shows] == ["The Hollow Coast"]


@respx.mock
async def test_a_show_with_no_ids_at_all_still_renders() -> None:
    """Nothing can be added from it, but a card that says so beats a missing row."""
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json=[
            {"watchers": 4, "show": {"title": "Orphan", "year": 2026}}
        ])
    )
    show = (await _client().trending_shows())[0]

    assert show.title == "Orphan"
    assert show.tvdb_id is None and show.tmdb_id is None
    assert show.addable is False


@respx.mock
async def test_a_dict_where_a_list_belongs_is_refused() -> None:
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, json={"error": "nope"})
    )
    with pytest.raises(TraktError):
        await _client().trending_shows()


# --- errors the Settings button has to name ---


@respx.mock
async def test_a_rejected_client_id_is_its_own_error_type() -> None:
    """"Trakt rejected the client ID" and "could not reach Trakt" are two different
    things to go and fix, so they are two different types."""
    for status in (401, 403):
        respx.get(f"{API}/shows/trending").mock(return_value=httpx.Response(status))
        with pytest.raises(TraktAuthError):
            await _client().trending_shows()


@respx.mock
async def test_connect_failure_maps_to_connection_error() -> None:
    respx.get(f"{API}/shows/trending").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(TraktConnectionError):
        await _client().trending_shows()


@respx.mock
async def test_a_server_error_is_a_plain_trakt_error() -> None:
    respx.get(f"{API}/shows/anticipated").mock(return_value=httpx.Response(503))
    with pytest.raises(TraktError) as raised:
        await _client().anticipated_shows()
    assert "503" in str(raised.value)


@respx.mock
async def test_a_non_json_200_maps_to_trakt_error() -> None:
    """A captive portal can answer any path with an HTML page."""
    respx.get(f"{API}/shows/trending").mock(
        return_value=httpx.Response(200, text="<html>hi</html>")
    )
    with pytest.raises(TraktError):
        await _client().trending_shows()


@respx.mock
async def test_a_transport_failure_leaves_no_credential_on_the_chain() -> None:
    """Trakt's URLs carry no secret, so this is not about the URL. It is that `scrub`
    drops the request object either way, and the header it holds DOES carry the client
    ID — an error reporter walking the chain would otherwise read it."""
    request = httpx.Request(
        "GET", f"{API}/shows/trending", headers={"trakt-api-key": CLIENT_ID}
    )
    respx.get(f"{API}/shows/trending").mock(
        side_effect=httpx.ConnectError("no route", request=request)
    )

    with pytest.raises(TraktError) as raised:
        await _client().trending_shows()

    original = raised.value.__context__
    assert original is not None, "nothing to scrub means this test proves nothing"
    with pytest.raises(RuntimeError):
        _ = original.request
