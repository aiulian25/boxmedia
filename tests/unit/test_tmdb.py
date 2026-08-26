"""TV step 6 test: the TMDB contract via respx, and the redaction that pays for it.

The last section is the one that matters most. TMDB's v3 API takes the key as a query
parameter, so every error path in that client is a chance to write a user's credential
into a log — and httpx puts the request URL into the text of most of its exceptions.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from app.services.discovery import TMDB_BASE_URL
from app.services.posters import POSTER_WIDTH, sized
from app.services.tmdb import (
    CAST_LIMIT,
    MAX_RETRY_AFTER_SECONDS,
    SORT_FIRST_AIR_DATE,
    TmdbAuthError,
    TmdbClient,
    TmdbConnectionError,
    TmdbError,
    TmdbRateLimitError,
    image_url,
)

API = TMDB_BASE_URL
KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

DISCOVER_ROW = {
    "id": 1396,
    "name": "The Hollow Coast",
    "first_air_date": "2023-01-20",
    "overview": "A survey team on a fault line that should not exist.",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    "vote_average": 8.4,
    "original_language": "en",
}

DETAIL = {
    "id": 1396,
    "name": "The Hollow Coast",
    "first_air_date": "2023-01-20",
    "overview": "A survey team on a fault line that should not exist.",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    "vote_average": 8.4,
    "genres": [{"id": 18, "name": "Drama"}, {"id": 9648, "name": "Mystery"}],
    "networks": [{"id": 49, "name": "HBO"}],
    "status": "Returning Series",
    "episode_run_time": [52],
    "number_of_seasons": 3,
    "number_of_episodes": 34,
    "in_production": True,
    "seasons": [
        {"season_number": 0, "name": "Specials", "episode_count": 2, "air_date": None},
        {"season_number": 1, "name": "Season 1", "episode_count": 10,
         "air_date": "2023-01-20", "poster_path": "/s1.jpg"},
    ],
    "external_ids": {"tvdb_id": 121361, "imdb_id": "tt0944947", "freebase_id": None},
    "content_ratings": {"results": [
        {"iso_3166_1": "GB", "rating": "15"},
        {"iso_3166_1": "US", "rating": "TV-MA"},
    ]},
    "credits": {"cast": [
        {"name": "Ines Aldaz", "character": "Dr. Petra Kohl", "profile_path": "/p1.jpg"},
        {"name": "Roland Mbeki", "character": "Survey Lead Osei", "profile_path": None},
    ]},
    "videos": {"results": [
        {"site": "YouTube", "type": "Teaser", "key": "teaser1"},
        {"site": "YouTube", "type": "Trailer", "key": "trailer1"},
    ]},
}


def _client() -> TmdbClient:
    return TmdbClient(KEY)


# --- image paths ---


def test_image_paths_become_urls_the_poster_cache_already_understands() -> None:
    """Composed at the width we render, not `original`: the cache keys on the URL, so
    two forms of one image would be two entries — and `original` is a 1-3 MB file
    painted into a 208px box."""
    url = image_url("/abc.jpg")
    assert url == f"https://image.tmdb.org/t/p/{POSTER_WIDTH}/abc.jpg"
    # And it survives `posters.sized` unchanged, so callers may keep routing through it.
    assert sized(url, POSTER_WIDTH) == url


def test_a_missing_or_odd_image_path_is_none_not_a_broken_url() -> None:
    for value in (None, "", "abc.jpg", 12, {"a": 1}):
        assert image_url(value) is None


# --- discover / search ---


@respx.mock
async def test_discover_maps_a_row() -> None:
    respx.get(f"{API}/discover/tv").mock(
        return_value=httpx.Response(200, json={"results": [DISCOVER_ROW]})
    )
    show = (await _client().discover_tv())[0]

    assert show.tmdb_id == 1396
    assert show.title == "The Hollow Coast"
    assert show.first_air_date == date(2023, 1, 20)
    assert show.year == 2023
    assert show.rating == 8.4
    assert show.poster_url.endswith(f"/{POSTER_WIDTH}/poster.jpg")
    assert show.backdrop_url.endswith("/w780/backdrop.jpg")


@respx.mock
async def test_discover_sends_the_filters_the_bar_exposes() -> None:
    route = respx.get(f"{API}/discover/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await _client().discover_tv(
        genre_id=18,
        first_air_date_from=date(2026, 1, 1),
        sort_by=SORT_FIRST_AIR_DATE,
        original_language="en",
        page=2,
    )

    params = route.calls.last.request.url.params
    assert params["with_genres"] == "18"
    assert params["first_air_date.gte"] == "2026-01-01"
    assert params["sort_by"] == SORT_FIRST_AIR_DATE
    assert params["with_original_language"] == "en"
    assert params["page"] == "2"
    # Never negotiable and never a setting: a filter you can turn off gets turned off.
    assert params["include_adult"] == "false"


@respx.mock
async def test_unset_filters_are_omitted_rather_than_sent_empty() -> None:
    """TMDB treats an empty `with_genres=` as a filter, not as "no filter"."""
    route = respx.get(f"{API}/discover/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await _client().discover_tv()

    params = route.calls.last.request.url.params
    assert "with_genres" not in params
    assert "first_air_date.gte" not in params


@respx.mock
async def test_an_unknown_sort_is_refused_before_anything_leaves() -> None:
    route = respx.get(f"{API}/discover/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    with pytest.raises(TmdbError, match="unknown sort option"):
        await _client().discover_tv(sort_by="chaos.desc")
    assert not route.called


@respx.mock
async def test_search_sends_the_term_and_an_empty_one_never_leaves() -> None:
    route = respx.get(f"{API}/search/tv").mock(
        return_value=httpx.Response(200, json={"results": [DISCOVER_ROW]})
    )
    assert (await _client().search_tv("hollow"))[0].tmdb_id == 1396
    assert route.calls.last.request.url.params["query"] == "hollow"

    assert await _client().search_tv("   ") == []
    assert route.call_count == 1  # the blank search was not a request


@respx.mock
async def test_a_show_with_no_air_date_is_not_an_error() -> None:
    """An anticipated series has no first air date. Common, and must not blank a row."""
    respx.get(f"{API}/discover/tv").mock(
        return_value=httpx.Response(
            200, json={"results": [{**DISCOVER_ROW, "first_air_date": ""}]}
        )
    )
    show = (await _client().discover_tv())[0]
    assert show.first_air_date is None
    assert show.year is None
    assert show.title == "The Hollow Coast"


@respx.mock
async def test_a_surprising_page_shape_is_refused() -> None:
    """A bare `for item in ...` over a dict silently yields its KEYS — wrong data rather
    than a caught error."""
    respx.get(f"{API}/discover/tv").mock(return_value=httpx.Response(200, json={"a": 1}))
    with pytest.raises(TmdbError):
        await _client().discover_tv()


# --- detail: the bridge, in one round trip ---


@respx.mock
async def test_detail_asks_for_everything_at_once() -> None:
    route = respx.get(f"{API}/tv/1396").mock(return_value=httpx.Response(200, json=DETAIL))
    await _client().tv_detail(1396)

    appended = route.calls.last.request.url.params["append_to_response"]
    # One request instead of the six the teardown found nzb360 fanning out.
    assert set(appended.split(",")) == {
        "external_ids", "content_ratings", "credits", "videos",
    }


@respx.mock
async def test_detail_extracts_the_tvdb_bridge() -> None:
    """The only thing that makes a Sonarr add possible from a TMDB-sourced row."""
    respx.get(f"{API}/tv/1396").mock(return_value=httpx.Response(200, json=DETAIL))
    detail = await _client().tv_detail(1396)

    assert detail.tvdb_id == 121361
    assert detail.imdb_id == "tt0944947"
    assert detail.addable is True


@respx.mock
async def test_a_show_with_no_tvdb_id_is_honestly_unaddable() -> None:
    """Not papered over with a guess: Sonarr keys on TVDB, and a wrong id adds the
    wrong series."""
    respx.get(f"{API}/tv/1396").mock(
        return_value=httpx.Response(
            200, json={**DETAIL, "external_ids": {"tvdb_id": None, "imdb_id": None}}
        )
    )
    detail = await _client().tv_detail(1396)

    assert detail.tvdb_id is None
    assert detail.addable is False


@respx.mock
async def test_detail_maps_the_rest_of_the_page() -> None:
    respx.get(f"{API}/tv/1396").mock(return_value=httpx.Response(200, json=DETAIL))
    detail = await _client().tv_detail(1396)

    assert detail.genres == ("Drama", "Mystery")
    assert detail.networks == ("HBO",)
    assert detail.status == "Returning Series"
    assert detail.episode_run_time == 52
    assert (detail.number_of_seasons, detail.number_of_episodes) == (3, 34)
    assert detail.in_production is True
    assert detail.trailer_url == "https://www.youtube.com/watch?v=trailer1"
    assert [season.season_number for season in detail.seasons] == [0, 1]
    assert detail.seasons[1].episode_count == 10
    assert [person.name for person in detail.cast] == ["Ines Aldaz", "Roland Mbeki"]
    assert detail.cast[0].role == "Dr. Petra Kohl"
    assert detail.cast[0].headshot_url.endswith("/w185/p1.jpg")
    # No profile picture is None, not a broken image.
    assert detail.cast[1].headshot_url is None


@respx.mock
async def test_the_certification_prefers_us_but_falls_back() -> None:
    """A British or Japanese show often carries no US rating at all, and "15" is more
    use than a blank."""
    respx.get(f"{API}/tv/1396").mock(return_value=httpx.Response(200, json=DETAIL))
    assert (await _client().tv_detail(1396)).certification == "TV-MA"

    respx.get(f"{API}/tv/2").mock(
        return_value=httpx.Response(200, json={
            **DETAIL, "content_ratings": {"results": [{"iso_3166_1": "GB", "rating": "15"}]},
        })
    )
    assert (await _client().tv_detail(2)).certification == "15"

    respx.get(f"{API}/tv/3").mock(
        return_value=httpx.Response(200, json={**DETAIL, "content_ratings": {"results": []}})
    )
    assert (await _client().tv_detail(3)).certification is None


@respx.mock
async def test_only_a_youtube_trailer_becomes_a_link() -> None:
    """A link out, never an embed: an iframe would need frame-src opened in the CSP, and
    the poster cache exists so that policy can stay shut."""
    respx.get(f"{API}/tv/1396").mock(
        return_value=httpx.Response(200, json={
            **DETAIL,
            "videos": {"results": [
                {"site": "Vimeo", "type": "Trailer", "key": "nope"},
                {"site": "YouTube", "type": "Featurette", "key": "nope2"},
            ]},
        })
    )
    assert (await _client().tv_detail(1396)).trailer_url is None


@respx.mock
async def test_the_cast_is_bounded() -> None:
    """TMDB returns the full unit — dozens for a long-running series."""
    respx.get(f"{API}/tv/1396").mock(
        return_value=httpx.Response(200, json={
            **DETAIL,
            "credits": {"cast": [
                {"name": f"Person {index}", "character": "Someone"}
                for index in range(CAST_LIMIT + 20)
            ]},
        })
    )
    assert len((await _client().tv_detail(1396)).cast) == CAST_LIMIT


@respx.mock
async def test_the_bridge_alone_is_a_smaller_answer() -> None:
    """An Add pressed on a discovery card already has the title and the poster."""
    respx.get(f"{API}/tv/1396/external_ids").mock(
        return_value=httpx.Response(200, json={"tvdb_id": 121361, "imdb_id": "tt0944947"})
    )
    assert await _client().tvdb_id_for(1396) == 121361


@respx.mock
async def test_the_bridge_alone_reports_a_missing_id_as_none() -> None:
    respx.get(f"{API}/tv/1396/external_ids").mock(
        return_value=httpx.Response(200, json={"tvdb_id": None})
    )
    assert await _client().tvdb_id_for(1396) is None


# --- rate limiting ---


@respx.mock
async def test_a_429_is_honoured_once_then_succeeds() -> None:
    route = respx.get(f"{API}/configuration").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"images": {}}),
        ]
    )
    assert await _client().configuration() == {"images": {}}
    assert route.call_count == 2


@respx.mock
async def test_a_second_429_gives_up_loudly_rather_than_sleeping_again() -> None:
    """The caller renders "try again shortly", which is true, instead of holding a
    request open hoping."""
    respx.get(f"{API}/configuration").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    with pytest.raises(TmdbRateLimitError):
        await _client().configuration()


@respx.mock
async def test_a_long_retry_after_is_capped_not_obeyed() -> None:
    """Sleeping 30s inside a page render is not a retry, it is a hang."""
    from app.services.tmdb import _retry_after

    response = httpx.Response(429, headers={"Retry-After": "600"})
    assert _retry_after(response) == MAX_RETRY_AFTER_SECONDS

    # A missing or unreadable header waits a little rather than not at all: hammering a
    # server that just said "slow down" is how a soft limit becomes a hard one.
    assert _retry_after(httpx.Response(429)) > 0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) > 0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "-5"})) == 0.0


# --- errors, and the credential that must never survive one ---


@respx.mock
async def test_unauthorized_maps_to_auth_error() -> None:
    respx.get(f"{API}/configuration").mock(return_value=httpx.Response(401))
    with pytest.raises(TmdbAuthError):
        await _client().configuration()


@respx.mock
async def test_connect_failure_maps_to_connection_error() -> None:
    respx.get(f"{API}/configuration").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(TmdbConnectionError):
        await _client().configuration()


@respx.mock
async def test_a_non_json_200_maps_to_tmdb_error() -> None:
    """A captive portal or a proxy can answer any path with an HTML page."""
    respx.get(f"{API}/configuration").mock(
        return_value=httpx.Response(200, text="<html>hello</html>")
    )
    with pytest.raises(TmdbError):
        await _client().configuration()


@respx.mock
async def test_the_key_is_sent_as_tmdb_requires() -> None:
    route = respx.get(f"{API}/configuration").mock(
        return_value=httpx.Response(200, json={})
    )
    await _client().configuration()

    request = route.calls.last.request
    # Their contract, not our choice — and the reason every error path is redacted.
    assert request.url.params["api_key"] == KEY
    assert request.headers["User-Agent"].startswith("BoxMedia/")
    assert request.url.params["language"] == "en-US"


@respx.mock
async def test_no_error_from_a_failing_call_carries_the_key() -> None:
    """The one that matters. httpx puts the request URL — which carries the key — into
    the text of most of its exceptions, so every message this client raises is redacted,
    and the transport errors raise `from None` so the original cannot ride the chain."""
    failures = [
        httpx.ConnectError(f"failed connecting to {API}/configuration?api_key={KEY}"),
        httpx.ReadTimeout(f"timed out reading {API}/configuration?api_key={KEY}"),
        httpx.ConnectTimeout(f"timed out connecting {API}/configuration?api_key={KEY}"),
    ]
    for failure in failures:
        respx.get(f"{API}/configuration").mock(side_effect=failure)
        with pytest.raises(TmdbError) as raised:
            await _client().configuration()

        assert KEY not in str(raised.value), failure
        assert KEY not in repr(raised.value), failure
        # And nothing chained behind it is carrying the raw text either.
        assert raised.value.__cause__ is None, failure
        assert KEY not in str(raised.value.__context__ or ""), failure


@respx.mock
async def test_no_error_from_a_bad_status_carries_the_key() -> None:
    for status in (400, 404, 500, 503):
        respx.get(f"{API}/tv/1").mock(return_value=httpx.Response(status))
        with pytest.raises(TmdbError) as raised:
            await _client().tv_detail(1)
        assert KEY not in str(raised.value)


@respx.mock
async def test_the_chained_original_carries_neither_the_text_nor_the_request() -> None:
    """`from None` suppresses the printed traceback but leaves __context__ populated, so
    anything walking the chain — a structured logger, an error reporter, a debugger repr
    — still reaches the original. httpx hangs the Request off it too, whose `.url` holds
    the key structurally rather than as text. Both are closed by `scrub`."""
    request = httpx.Request("GET", f"{API}/configuration?api_key={KEY}")
    respx.get(f"{API}/configuration").mock(
        side_effect=httpx.ConnectError(
            f"no route to {API}/configuration?api_key={KEY}", request=request
        )
    )

    with pytest.raises(TmdbError) as raised:
        await _client().configuration()

    original = raised.value.__context__
    assert original is not None, "nothing to scrub means this test proves nothing"
    assert KEY not in str(original)
    assert KEY not in repr(original.args)
    with pytest.raises(RuntimeError):
        _ = original.request  # dropped, so it answers with a failure rather than a key
