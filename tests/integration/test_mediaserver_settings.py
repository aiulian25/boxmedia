"""Media server (P1/J1): the Settings card, the routes, and the cards' annotation."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.mediaserver import (
    KIND_JELLYFIN,
    KIND_PLEX,
    LIBRARY_CACHE_TTL_SECONDS,
    MEDIA_SERVER_FILENAME,
    MediaServerFetch,
    MediaServerMovie,
    MediaServerSeries,
)
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.web.deps import MEDIA_SERVER_BACKOFF_KEY
from tests.conftest import AppHarness

PLEX_URL = "http://plex.local:32400"
TOKEN = "plex-token-for-tests"  # noqa: S105 — the suite's dummy token

SECTIONS = {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}


def _movies_body(*items: dict) -> dict:
    return {"MediaContainer": {"Metadata": list(items), "size": len(items),
                               "totalSize": len(items)}}


def _plex_item(title: str, year: int, tmdb: int | None) -> dict:
    item: dict = {"title": title, "year": year, "guid": f"plex://movie/{title}"}
    if tmdb is not None:
        item["Guid"] = [{"id": f"tmdb://{tmdb}"}]
    return item


def _connect(harness: AppHarness, *, kind: str = KIND_PLEX, url: str = PLEX_URL) -> None:
    harness.client.post(
        "/settings/media-server",
        data={"url": url, "token": TOKEN, "kind": kind},
        follow_redirects=False,
    )


def _report_with_missing_title(harness: AppHarness, *, tmdb: int, year: int) -> str:
    report_id = "report-20260818-090000-plex"
    harness.client.app.state.reports.save(Report(
        id=report_id, run_at="2026-08-18T09:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W33", totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=5_000_000, gross_display="$5.0M", weeks_in_release=1,
            status=MovieStatus.MISSING, action=MovieAction.NONE,
            tmdb_id=tmdb, year=year)],
    ))
    return report_id


# --- the settings card ---


def test_the_card_offers_connect_until_a_server_is_saved(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/settings").text

    assert "Media Server" in page
    assert "Connect" in page
    assert "only ever <strong>reads</strong> it" in page


def test_saving_masks_the_token_and_never_echoes_it(harness: AppHarness) -> None:
    harness.activate()

    _connect(harness)
    page = harness.client.get("/settings").text

    assert TOKEN not in page
    assert "••••" in page
    assert "Refresh Library" in page  # the saved-state controls appear


def test_the_token_is_encrypted_on_disk(harness: AppHarness) -> None:
    harness.activate()

    _connect(harness)

    raw = (harness.settings.config_dir / MEDIA_SERVER_FILENAME).read_text(encoding="utf-8")
    assert TOKEN not in raw


def test_a_bad_url_is_refused(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/media-server", data={"url": "   ", "token": TOKEN}, follow_redirects=False
    )

    assert "server_invalid" in response.headers["location"]
    assert harness.client.app.state.media_server.load() is None


def test_remove_forgets_the_connection_and_the_snapshot(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    harness.client.post("/settings/media-server/remove", follow_redirects=False)

    assert harness.client.app.state.media_server.load() is None
    # A library nobody is connected to must not keep decorating cards.
    assert harness.client.app.state.media_server_cache.load() is None


@respx.mock
def test_test_connection_reports_each_of_its_three_answers(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    route = respx.get(f"{PLEX_URL}/library/sections")

    route.mock(return_value=httpx.Response(200, json=SECTIONS))
    ok = harness.client.post("/settings/media-server/test", follow_redirects=False)
    assert "server_test_ok" in ok.headers["location"]

    route.mock(return_value=httpx.Response(401))
    auth = harness.client.post("/settings/media-server/test", follow_redirects=False)
    assert "server_test_auth" in auth.headers["location"]

    route.mock(side_effect=httpx.ConnectError("down"))
    conn = harness.client.post("/settings/media-server/test", follow_redirects=False)
    assert "server_test_conn" in conn.headers["location"]


@respx.mock
def test_refresh_fetches_now_and_caches(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=SECTIONS)
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_movies_body(
            _plex_item("Neon Rain", 2026, 52001)
        ))
    )

    response = harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "server_refreshed" in response.headers["location"]
    cached = harness.client.app.state.media_server_cache.load()
    assert cached is not None
    assert cached[0].holds(52001, None, "x", None) == "yes"


# --- the cards ---


def test_a_missing_title_plex_holds_says_so_confidently(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)

    before = _actions_block(harness.client.get(f"/reports/{report_id}").text)

    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(
            title="Different Spelling Entirely", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))
    page = harness.client.get(f"/reports/{report_id}").text

    assert "Already in Plex" in page
    assert "server-hint" in page
    # The id matched even though the spellings differ — that is what makes it confident.
    # And the hint informs without removing anything: the card offers exactly the same
    # actions it offered before Plex had an opinion.
    assert _actions_block(page) == before


def _actions_block(page: str) -> str:
    """Everything from the card's action controls to the end of the page.

    The hint is injected ABOVE the actions div, so if it changes anything at all about
    what the card offers, this suffix stops being byte-identical.
    """
    return page[page.index('<div class="poster-actions">'):]


def test_a_title_match_renders_as_the_educated_guess(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=None, imdb_id=None),
    ), truncated=False))

    page = harness.client.get(f"/reports/{report_id}").text

    assert "Probably in Plex — verify" in page
    assert "Already in Plex" not in page


def test_a_title_plex_does_not_hold_gets_no_hint(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(), truncated=False))

    page = harness.client.get(f"/reports/{report_id}").text

    assert "in Plex" not in page


def test_without_a_connection_the_page_renders_exactly_as_before(
    harness: AppHarness,
) -> None:
    harness.activate()
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)

    page = harness.client.get(f"/reports/{report_id}").text

    assert "in Plex" not in page
    assert "Neon Rain" in page


@respx.mock
def test_renders_inside_the_ttl_never_touch_plex(harness: AppHarness) -> None:
    """The acceptance criterion verbatim: two consecutive renders, zero requests."""
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))
    route = respx.get(url__regex=r"http://plex\.local.*").mock(
        return_value=httpx.Response(200, json=SECTIONS)
    )

    harness.client.get(f"/reports/{report_id}")
    harness.client.get(f"/reports/{report_id}")

    assert route.call_count == 0
    assert LIBRARY_CACHE_TTL_SECONDS >= 300, "a TTL this short would defeat the cache"


@respx.mock
def test_a_down_plex_renders_the_stale_snapshot_not_an_error(
    harness: AppHarness, monkeypatch,
) -> None:
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))
    # Age the cache past the TTL so the render tries to refresh, against a dead host.
    import app.web.deps as deps
    monkeypatch.setattr(deps.time, "time", lambda: 4_000_000_000.0)
    respx.get(url__regex=r"http://plex\.local.*").mock(side_effect=httpx.ConnectError("down"))

    page = harness.client.get(f"/reports/{report_id}")

    assert page.status_code == 200
    assert "Already in Plex" in page.text, "the stale snapshot should still answer"


def test_the_report_file_is_untouched_by_annotation(harness: AppHarness) -> None:
    """The acceptance criterion: presence is computed at render, never stored."""
    harness.activate()
    _connect(harness)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))
    path = harness.settings.history_dir / f"{report_id}.json"
    before = path.read_bytes()

    harness.client.get(f"/reports/{report_id}")

    assert path.read_bytes() == before


def test_a_title_radarr_already_tracks_gets_no_hint(harness: AppHarness) -> None:
    """The gate, from the other side: a title any Radarr holds or wants already tells
    its story through the badge, and stacking "In Plex" on top would be noise."""
    harness.activate()
    _connect(harness)
    report_id = "report-20260818-091000-plix"
    harness.client.app.state.reports.save(Report(
        id=report_id, run_at="2026-08-18T09:10:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W33", totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=5_000_000, gross_display="$5.0M", weeks_in_release=1,
            status=MovieStatus.WANTED, action=MovieAction.NONE,
            tmdb_id=52001, year=2026)],
    ))
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    page = harness.client.get(f"/reports/{report_id}").text

    assert "in Plex" not in page


def _dashboard_card(harness: AppHarness, status: str) -> None:
    harness.client.app.state.reports.save(Report(
        id="report-20260818-092000-dash", run_at="2026-08-18T09:20:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W33",
        totals=ReportTotals(movies=1, matched=1),
        movies=[MovieResult(
            rank=1, title="Neon Rain", normalized_title="neon rain",
            gross_amount=5_000_000, gross_display="$5.0M", weeks_in_release=1,
            status=status, action=MovieAction.NONE, tmdb_id=52001, year=2026)],
    ))
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))


def test_the_dashboard_hints_only_on_wanted_titles(harness: AppHarness) -> None:
    """Waiting on a download of something Plex can already play is worth a chip; for a
    file Radarr holds, Radarr holding it is the stronger, more specific statement."""
    harness.activate()
    _connect(harness)

    _dashboard_card(harness, MovieStatus.WANTED)
    assert "Already in Plex" in harness.client.get("/library").text

    _dashboard_card(harness, MovieStatus.IN_LIBRARY)
    assert "in Plex" not in harness.client.get("/library").text


# --- testing before saving, the way the Radarr card always has ---


@respx.mock
def test_a_plex_connection_can_be_tested_before_it_is_saved(harness: AppHarness) -> None:
    """The gap this closes: Plex shipped with only the after-saving test, which asked
    you to commit a credential to disk before you could learn whether it works."""
    harness.activate()
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=SECTIONS)
    )

    response = harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": TOKEN}
    )

    assert response.status_code == 200
    assert "Plex responded" in response.text
    assert harness.client.app.state.media_server.load() is None, "a test must store nothing"


@respx.mock
def test_the_pre_save_test_reports_each_failure_in_its_own_words(
    harness: AppHarness,
) -> None:
    harness.activate()
    route = respx.get(f"{PLEX_URL}/library/sections")

    route.mock(return_value=httpx.Response(401))
    assert "rejected the credential" in harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": TOKEN}
    ).text

    route.mock(side_effect=httpx.ConnectError("down"))
    assert "Could not reach it" in harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": TOKEN}
    ).text

    # Reachable and authenticated, but nothing to read: every later fetch would return
    # nothing and the cards would silently never mention Plex. That is a failed test.
    route.mock(return_value=httpx.Response(200, json={"MediaContainer": {"Directory": [
        {"key": "2", "type": "show"},
    ]}}))
    assert "no movie library" in harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": TOKEN}
    ).text


def test_an_unreadable_address_is_named_as_such(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/media-server/test-credentials", data={"url": "   ", "token": TOKEN}
    )

    assert "check the address" in response.text


@respx.mock
def test_testing_an_address_change_may_reuse_the_stored_token(harness: AppHarness) -> None:
    """The blank-token contract, applied to the test as well as the save: changing only
    the address must not force re-pasting a secret just to check it."""
    harness.activate()
    _connect(harness)
    seen: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Plex-Token", ""))
        return httpx.Response(200, json=SECTIONS)

    respx.get(f"{PLEX_URL}/library/sections").mock(side_effect=_capture)

    response = harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": ""}
    )

    assert "Plex responded" in response.text
    assert seen == [TOKEN], "the stored token should have been used"


def test_the_pre_save_test_needs_a_token_when_none_is_stored(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/media-server/test-credentials", data={"url": PLEX_URL, "token": ""}
    )

    assert "rejected the credential" in response.text


# --- Jellyfin through the same card and the same cards (J1) ---

JELLYFIN_URL = "http://jellyfin.local:8096"


def _jellyfin_items(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"Items": items, "TotalRecordCount": len(items)})


def test_the_card_names_both_servers_before_one_is_chosen(harness: AppHarness) -> None:
    """Which server it is has to be answerable from the card itself, not guessed from
    the port someone happens to type."""
    harness.activate()

    page = harness.client.get("/settings").text

    assert 'value="plex"' in page and 'value="jellyfin"' in page
    assert "Which server" in page
    # Both defaults are named while nothing is chosen, so neither user is left guessing.
    assert "32400" in page and "8096" in page


def test_a_jellyfin_connection_says_jellyfin_everywhere(harness: AppHarness) -> None:
    harness.activate()

    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)
    page = harness.client.get("/settings").text

    assert "Connected to Jellyfin" in page
    assert "Test Jellyfin" in page
    assert "Jellyfin API key" in page or "API key" in page
    assert harness.client.app.state.media_server.load().kind == KIND_JELLYFIN


@respx.mock
def test_a_jellyfin_connection_is_tested_before_saving(harness: AppHarness) -> None:
    harness.activate()
    respx.get(f"{JELLYFIN_URL}/Items").mock(
        return_value=httpx.Response(200, json={"Items": [], "TotalRecordCount": 3})
    )

    response = harness.client.post(
        "/settings/media-server/test-credentials",
        data={"url": JELLYFIN_URL, "token": "key", "kind": KIND_JELLYFIN},
    )

    assert "Jellyfin responded" in response.text
    assert harness.client.app.state.media_server.load() is None, "a test must store nothing"


@respx.mock
def test_a_rejected_jellyfin_key_names_jellyfin(harness: AppHarness) -> None:
    harness.activate()
    respx.get(f"{JELLYFIN_URL}/Items").mock(return_value=httpx.Response(401))

    response = harness.client.post(
        "/settings/media-server/test-credentials",
        data={"url": JELLYFIN_URL, "token": "wrong", "kind": KIND_JELLYFIN},
    )

    assert "Jellyfin rejected the credential" in response.text


def test_a_kind_this_build_does_not_ship_is_refused_by_the_form(
    harness: AppHarness,
) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/media-server",
        data={"url": JELLYFIN_URL, "token": "key", "kind": "emby"},
        follow_redirects=False,
    )

    assert "server_invalid" in response.headers["location"]
    assert harness.client.app.state.media_server.load() is None


def test_the_cards_say_jellyfin_when_that_is_what_is_connected(
    harness: AppHarness,
) -> None:
    """The whole feature, on the other server: the snapshot path is server-neutral, so
    only the name on the chip should differ."""
    harness.activate()
    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)
    report_id = _report_with_missing_title(harness, tmdb=52001, year=2026)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Different Spelling", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    page = harness.client.get(f"/reports/{report_id}").text

    assert "Already in Jellyfin" in page
    assert "Already in Plex" not in page


def test_switching_server_drops_the_previous_library(harness: AppHarness) -> None:
    """Another server's films must not keep decorating cards for up to a TTL."""
    harness.activate()
    _connect(harness)
    harness.client.app.state.media_server_cache.save(MediaServerFetch(movies=(
        MediaServerMovie(title="Neon Rain", year=2026, tmdb_id=52001, imdb_id=None),
    ), truncated=False))

    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)

    assert harness.client.app.state.media_server_cache.load() is None


@respx.mock
def test_a_jellyfin_library_refreshes_through_the_same_button(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)
    respx.get(f"{JELLYFIN_URL}/Items").mock(return_value=_jellyfin_items([
        {"Name": "Neon Rain", "ProductionYear": 2026,
         "ProviderIds": {"Tmdb": "52001", "TmdbCollection": "9999"}},
    ]))

    response = harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "server_refreshed" in response.headers["location"]
    cached = harness.client.app.state.media_server_cache.load()
    assert cached[0].holds(52001, None, "x", None) == "yes"
    # And the collection id did not become a film, end to end.
    assert cached[0].holds(9999, None, "x", None) is None


def test_a_stored_token_that_will_not_decrypt_blames_neither_the_server_nor_the_address(
    harness: AppHarness,
) -> None:
    """Testing with the field left blank reaches `decrypt_token` WITHOUT going through
    `build_client`, so this path needs the mapping in its own right.

    The verdict is its own: nothing was asked of the server, so "rejected the credential"
    and "could not reach it" would both be claims about a request never made. No respx
    mock, so a regression that started probing would fail on an unmocked request.
    """
    from app.core import crypto
    from app.services.mediaserver import MediaServerStore

    harness.activate()
    _connect(harness)
    harness.client.app.state.media_server = MediaServerStore(
        harness.settings.config_dir, key=crypto.generate_key()
    )

    response = harness.client.post(
        "/settings/media-server/test-credentials",
        data={"url": PLEX_URL, "token": "", "kind": KIND_PLEX},
    )

    assert "can’t be read" in response.text
    assert "rejected the credential" not in response.text
    assert "Could not reach it" not in response.text


# --- the Refresh button and television ---

KNOWN_FILM_TMDB = 52001
KNOWN_SHOW_TVDB = 121361
SONARR_URL_FOR_TESTS = "http://sonarr.local:8989"
SONARR_KEY_FOR_TESTS = "0123456789abcdef0123456789abcdef"
MIXED_SECTIONS = {"MediaContainer": {"Directory": [
    {"key": "1", "type": "movie"}, {"key": "2", "type": "show"},
]}}


def _mock_plex_library(*, tv_status: int = 200) -> respx.Route:
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=MIXED_SECTIONS)
    )
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_movies_body(
            _plex_item("Neon Rain", 2026, KNOWN_FILM_TMDB)
        ))
    )
    show = {
        "title": "The Hollow Coast", "year": 2023, "guid": "plex://show/hollow",
        "Guid": [{"id": f"tvdb://{KNOWN_SHOW_TVDB}"}],
    }
    return respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=httpx.Response(tv_status, json=_movies_body(show))
    )


def _mock_jellyfin_library(*, tv_status: int = 200) -> list[str]:
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["IncludeItemTypes"]
        asked.append(kind)
        if kind == "Series":
            if tv_status != 200:
                return httpx.Response(tv_status)
            return _jellyfin_items([{
                "Name": "The Hollow Coast", "ProductionYear": 2023,
                "ProviderIds": {"Tvdb": str(KNOWN_SHOW_TVDB)},
            }])
        return _jellyfin_items([{
            "Name": "Neon Rain", "ProductionYear": 2026,
            "ProviderIds": {"Tmdb": str(KNOWN_FILM_TMDB)},
        }])

    respx.get(f"{JELLYFIN_URL}/Items").mock(side_effect=answer)
    return asked


def _connect_provider(harness: AppHarness, provider: str, *, tv_status: int = 200) -> None:
    if provider == KIND_PLEX:
        _connect(harness)
        _mock_plex_library(tv_status=tv_status)
        return
    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)
    _mock_jellyfin_library(tv_status=tv_status)


def _with_sonarr(harness: AppHarness) -> None:
    harness.client.app.state.apps.add(
        name="Sonarr", url=SONARR_URL_FOR_TESTS, api_key=SONARR_KEY_FOR_TESTS, kind="sonarr"
    )


def _seed_known_library(harness: AppHarness) -> None:
    harness.client.app.state.media_server_cache.save(MediaServerFetch(
        movies=(MediaServerMovie(
            title="Old Film", year=2020, tmdb_id=KNOWN_FILM_TMDB + 1, imdb_id=None
        ),),
        truncated=False,
        series=(MediaServerSeries(
            title="Old Show", year=2020, tvdb_id=KNOWN_SHOW_TVDB + 1, tmdb_id=None, imdb_id=None
        ),),
    ))


def _holds_show(harness: AppHarness, tvdb_id: int) -> str | None:
    snapshot = harness.client.app.state.media_server_cache.load()[0]
    return snapshot.holds_series(tvdb_id, None, None, "x", None)


def _holds_film(harness: AppHarness, tmdb_id: int) -> str | None:
    snapshot = harness.client.app.state.media_server_cache.load()[0]
    return snapshot.holds(tmdb_id, None, "x", None)


@pytest.mark.parametrize("provider", [KIND_PLEX, KIND_JELLYFIN])
@respx.mock
def test_refresh_reads_films_and_shows_together(harness: AppHarness, provider: str) -> None:
    """The audit's reproduction: the button read films only and saved that as everything, so
    a show the server held stopped being known, with a fresh timestamp on the loss."""
    harness.activate()
    _connect_provider(harness, provider)
    _with_sonarr(harness)

    response = harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "server_refreshed" in response.headers["location"]
    assert _holds_film(harness, KNOWN_FILM_TMDB) == "yes"
    assert _holds_show(harness, KNOWN_SHOW_TVDB) == "yes"


@pytest.mark.parametrize("provider", [KIND_PLEX, KIND_JELLYFIN])
@respx.mock
def test_a_tv_failure_erases_nothing(harness: AppHarness, provider: str) -> None:
    harness.activate()
    _connect_provider(harness, provider, tv_status=500)
    _with_sonarr(harness)
    _seed_known_library(harness)
    before = harness.client.app.state.media_server_cache.load()[1]

    response = harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "server_refresh_failed" in response.headers["location"]
    assert _holds_show(harness, KNOWN_SHOW_TVDB + 1) == "yes"
    assert _holds_film(harness, KNOWN_FILM_TMDB + 1) == "yes"
    assert harness.client.app.state.media_server_cache.load()[1] == before  # not stamped fresh


@respx.mock
def test_without_a_sonarr_refresh_does_not_walk_plex_tv(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness)
    shows = _mock_plex_library()

    response = harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "server_refreshed" in response.headers["location"]
    assert not shows.called
    assert _holds_film(harness, KNOWN_FILM_TMDB) == "yes"


@respx.mock
def test_without_a_sonarr_refresh_does_not_walk_jellyfin_tv(harness: AppHarness) -> None:
    harness.activate()
    _connect(harness, kind=KIND_JELLYFIN, url=JELLYFIN_URL)
    asked = _mock_jellyfin_library()

    harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert "Series" not in asked
    assert _holds_film(harness, KNOWN_FILM_TMDB) == "yes"


@respx.mock
def test_a_failed_refresh_leaves_pages_alone_for_a_while_like_any_other(
    harness: AppHarness,
) -> None:
    harness.activate()
    _connect(harness)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(500))

    harness.client.post("/settings/media-server/refresh", follow_redirects=False)

    assert harness.client.app.state.media_server_backoff.should_skip(MEDIA_SERVER_BACKOFF_KEY)

