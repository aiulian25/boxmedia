"""Media server (P1/J1): the Settings card, the routes, and the cards' annotation."""

from __future__ import annotations

import httpx
import respx

from app.services.mediaserver import (
    KIND_JELLYFIN,
    KIND_PLEX,
    LIBRARY_CACHE_TTL_SECONDS,
    MEDIA_SERVER_FILENAME,
    MediaServerFetch,
    MediaServerMovie,
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
    assert "Already in Plex" in harness.client.get("/dashboard").text

    _dashboard_card(harness, MovieStatus.IN_LIBRARY)
    assert "in Plex" not in harness.client.get("/dashboard").text


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
