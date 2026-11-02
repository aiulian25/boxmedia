"""The Upcoming Films shelf: off by default, fetched only when asked, and never about money."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from app.services.discovery import PROVIDER_TMDB, TMDB_BASE_URL
from app.services.ignore import KIND_SERIES
from app.services.radarr import RadarrMovie
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.upcoming import (
    MAX_CACHE_AGE_SECONDS,
    UPCOMING_CACHE_FILENAME,
    UpcomingCache,
    UpcomingFilm,
)
from tests.conftest import AppHarness

DISCOVER_URL = f"{TMDB_BASE_URL}/discover/movie"
TMDB_KEY = "tmdbkeyfortests0123456789"
FILM_ID = 7001
OTHER_ID = 7002
RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
REFRESH = "/discover/upcoming/refresh"


def _soon(days: int = 10) -> date:
    return datetime.now(UTC).date() + timedelta(days=days)


def _result(tmdb_id: int = FILM_ID, title: str = "Paper Comet", days: int = 10) -> dict:
    return {
        "id": tmdb_id, "title": title, "release_date": _soon(days).isoformat(),
        "poster_path": None, "overview": "…", "vote_average": 0,
    }


def _tmdb_answers(*results: dict) -> respx.Route:
    return respx.get(DISCOVER_URL).mock(
        return_value=httpx.Response(200, json={"page": 1, "results": list(results)})
    )


def _turn_on(harness: AppHarness, territory: str = "GB", days: str = "60") -> None:
    response = harness.client.post(
        "/settings/upcoming",
        data={
            "upcoming_enabled": "on", "upcoming_territory": territory,
            "upcoming_window_days": days,
        },
        follow_redirects=False,
    )
    assert "upcoming_saved" in response.headers["location"]


def _ready(harness: AppHarness, **kwargs: str) -> None:
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TMDB, TMDB_KEY)
    _turn_on(harness, **kwargs)


def _refresh(harness: AppHarness) -> str:
    return harness.client.post(REFRESH, follow_redirects=False).headers["location"]


def _seed(harness: AppHarness, *films: UpcomingFilm) -> None:
    harness.client.app.state.upcoming_cache.save(films, "GB", 60)


def _film(tmdb_id: int = FILM_ID, title: str = "Paper Comet") -> UpcomingFilm:
    return UpcomingFilm(tmdb_id, title, _soon())


def _page(harness: AppHarness) -> str:
    return harness.client.get("/discover?type=movies").text


# --- opt-in ---


@respx.mock
def test_it_is_off_by_default_and_asks_nothing(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TMDB, TMDB_KEY)

    assert "Upcoming films" not in _page(harness)
    assert "upcoming_off" in _refresh(harness)
    assert len(respx.calls) == 0
    assert harness.client.app.state.filters.load().upcoming_enabled is False


@respx.mock
def test_turning_it_on_fetches_nothing_until_refresh_is_pressed(harness: AppHarness) -> None:
    _ready(harness)

    page = _page(harness)

    assert "Upcoming films" in page
    assert "Nothing fetched yet" in page
    assert len(respx.calls) == 0


@respx.mock
def test_opening_any_page_after_a_refresh_still_asks_nothing(harness: AppHarness) -> None:
    _ready(harness)
    route = _tmdb_answers(_result())
    _refresh(harness)
    assert route.call_count == 1

    for path in ("/discover", "/discover?type=movies", "/settings", "/library", "/reports"):
        harness.client.get(path)

    assert route.call_count == 1


def test_only_the_refresh_route_ever_calls_it() -> None:
    """The restart half of the promise: no job, no startup hook, no page can reach it."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    callers = [
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if ".upcoming_films(" in path.read_text(encoding="utf-8")
    ]

    assert callers == ["web/discover.py"]


def test_the_tv_chip_has_no_film_shelf(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    assert "Paper Comet" not in harness.client.get("/discover?type=tv").text


# --- what it asks ---


@respx.mock
def test_refresh_asks_for_the_chosen_territory_and_window_once(harness: AppHarness) -> None:
    _ready(harness, territory="DE", days="30")
    route = _tmdb_answers(_result())

    assert "upcoming_refreshed" in _refresh(harness)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["region"] == "DE"
    assert params["release_date.gte"] == _soon(0).isoformat()
    assert params["release_date.lte"] == _soon(30).isoformat()
    assert params["with_release_type"] == "3|2"
    assert params["include_adult"] == "false"


@respx.mock
def test_a_film_with_no_date_or_outside_the_window_is_not_kept(harness: AppHarness) -> None:
    _ready(harness)
    _tmdb_answers(
        _result(title="Kept"),
        _result(OTHER_ID, "Too Late", days=400),
        {**_result(7003, "No Date"), "release_date": ""},
        {"title": "No Id", "release_date": _soon().isoformat()},
        "not a row",
    )

    _refresh(harness)

    shelf = harness.client.app.state.upcoming_cache.load()
    assert [film.title for film in shelf.films] == ["Kept"]


@respx.mock
def test_refresh_is_audited(harness: AppHarness) -> None:
    _ready(harness)
    _tmdb_answers(_result())

    _refresh(harness)

    log = (harness.settings.logs_dir / "audit.jsonl").read_text("utf-8")
    assert "upcoming_refreshed" in log
    assert TMDB_KEY not in log


# --- failing ---


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"results": "nope"}),
        httpx.ConnectError("gone"),
    ],
    ids=["500", "401", "list", "bad-results", "unreachable"],
)
@respx.mock
def test_a_failure_keeps_the_last_shelf_and_says_so(harness: AppHarness, answer: object) -> None:
    _ready(harness)
    _seed(harness, _film())
    route = respx.get(DISCOVER_URL)
    route.mock(side_effect=answer) if isinstance(answer, Exception) else route.mock(
        return_value=answer
    )

    location = _refresh(harness)

    assert "upcoming_refresh_failed" in location
    assert TMDB_KEY not in location
    assert "Paper Comet" in _page(harness)


@respx.mock
def test_no_key_says_so_and_asks_nothing(harness: AppHarness) -> None:
    harness.activate()
    _turn_on(harness)

    assert "upcoming_no_key" in _refresh(harness)
    assert "Needs your" in _page(harness)
    assert len(respx.calls) == 0


# --- what it shows ---


def test_the_shelf_names_its_source_territory_window_and_age(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    page = _page(harness)

    assert "TMDB · United Kingdom · next 60 days · fetched " in page
    assert "not endorsed, certified, or otherwise approved by TMDB" in page


def test_dates_are_day_first_and_provisional(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())
    released = _soon()

    page = _page(harness)

    assert f"Expected {released.day}/{released.month}/{released.year}" in page
    assert "can move" in page


def test_the_cards_claim_no_money(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TMDB, TMDB_KEY)
    _turn_on(harness)
    _seed(harness, _film())

    section = _page(harness).split("Upcoming films", 1)[1].split("</section>", 1)[0]

    assert "$" not in section
    assert "gross" not in re.sub(r'class="[^"]*"', "", section).lower()


def test_changing_settings_says_the_shelf_is_from_the_old_ones(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())
    _turn_on(harness, territory="FR")

    page = _page(harness)

    assert "United Kingdom" in page  # still labelled as what it is
    assert "Refresh to apply" in page


# --- against what is already known ---


def _chart_with(harness: AppHarness, tmdb_id: int) -> None:
    harness.client.app.state.reports.save(Report(
        id="report-2026W32-chart", week="2026W32", run_at="2026-08-19T23:44:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.OK,
        totals=ReportTotals(movies=1, matched=0),
        movies=[MovieResult(
            rank=1, title="Charted Film", normalized_title="charted film",
            gross_amount=1, gross_display="$1", weeks_in_release=1,
            status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=tmdb_id,
        )],
    ))


def _section(harness: AppHarness) -> str:
    return _page(harness).split("Upcoming films", 1)[1].split("</section>", 1)[0]


def test_a_film_already_on_a_chart_is_left_out_and_counted(harness: AppHarness) -> None:
    _ready(harness)
    _chart_with(harness, FILM_ID)
    _seed(harness, _film(), _film(OTHER_ID, "Still Ahead"))

    section = _section(harness)

    assert "Paper Comet" not in section
    assert "Still Ahead" in section
    assert "1 already charted or ignored" in section


def test_an_ignored_film_is_left_out(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())
    harness.client.app.state.ignore.add(
        tmdb_id=FILM_ID, title="Paper Comet", normalized_title="paper comet"
    )

    assert "Paper Comet" not in _section(harness)


def test_an_ignored_series_with_the_same_number_does_not_hide_a_film(
    harness: AppHarness,
) -> None:
    _ready(harness)
    _seed(harness, _film())
    harness.client.app.state.ignore.add(
        tmdb_id=FILM_ID, title="A Show", normalized_title="a show", kind=KIND_SERIES
    )

    assert "Paper Comet" in _section(harness)


def test_with_nothing_read_a_film_is_unknown_and_not_missing(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    section = _section(harness)

    assert ">Unknown<" in section
    assert ">Missing<" not in section


@pytest.mark.parametrize(
    ("library", "label"),
    [
        ({}, "Missing"),
        ({FILM_ID: RadarrMovie(FILM_ID, "Paper Comet", 2026, has_file=True)}, "In Library"),
        ({FILM_ID: RadarrMovie(FILM_ID, "Paper Comet", 2026, has_file=False)}, "Wanted"),
    ],
    ids=["missing", "in-library", "wanted"],
)
def test_a_recent_read_says_what_is_held(
    harness: AppHarness, library: dict, label: str
) -> None:
    _ready(harness)
    _seed(harness, _film())
    state = harness.client.app.state
    app = state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    state.radarr_library_snapshots.put(app.id, library)

    assert f">{label}<" in _section(harness)


# --- no automatic anything ---


@respx.mock
def test_refresh_changes_nothing_in_radarr_and_a_card_opens_the_film(harness: AppHarness) -> None:
    """The only route to an Add is the film's own page, where a target is chosen."""
    _ready(harness)
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _tmdb_answers(_result())

    _refresh(harness)
    section = _section(harness)

    assert [call.request.url.host for call in respx.calls] == ["api.themoviedb.org"]
    assert f'href="/movies/{FILM_ID}" data-movie="{FILM_ID}"' in section
    assert "add-movie" not in section


# --- keeping it ---


def test_turning_it_off_drops_what_was_fetched(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    harness.client.post(
        "/settings/upcoming",
        data={"upcoming_territory": "GB", "upcoming_window_days": "60"},
        follow_redirects=False,
    )

    state = harness.client.app.state
    assert state.filters.load().upcoming_enabled is False
    assert state.upcoming_cache.load() is None
    assert "Upcoming films" not in _page(harness)


def test_deleting_the_tmdb_key_drops_what_it_fetched(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    harness.client.post("/settings/discovery/tmdb/delete", follow_redirects=False)

    assert harness.client.app.state.upcoming_cache.load() is None


def test_deleting_the_trakt_key_leaves_it(harness: AppHarness) -> None:
    _ready(harness)
    _seed(harness, _film())

    harness.client.post("/settings/discovery/trakt/delete", follow_redirects=False)

    assert harness.client.app.state.upcoming_cache.load() is not None


def _age_by(tmp_path: Path, seconds: float) -> None:
    path = tmp_path / UPCOMING_CACHE_FILENAME
    document = json.loads(path.read_text("utf-8"))
    document["fetched_at"] = time.time() - seconds
    path.write_text(json.dumps(document), "utf-8")


def test_a_cache_past_tmdbs_six_months_is_not_shown(tmp_path: Path) -> None:
    cache = UpcomingCache(tmp_path)
    cache.save((_film(),), "GB", 60)

    _age_by(tmp_path, MAX_CACHE_AGE_SECONDS - 60)
    assert cache.load() is not None

    _age_by(tmp_path, MAX_CACHE_AGE_SECONDS + 60)
    assert cache.load() is None


def test_a_damaged_cache_is_no_cache_and_a_damaged_row_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / UPCOMING_CACHE_FILENAME
    cache = UpcomingCache(tmp_path)
    assert cache.load() is None

    path.write_text("not json", "utf-8")
    assert cache.load() is None

    cache.save((_film(),), "GB", 60)
    document = json.loads(path.read_text("utf-8"))
    document["films"].extend([1, {"tmdb_id": "x"}, {"tmdb_id": 3, "title": "T", "release_date": 5}])
    path.write_text(json.dumps(document), "utf-8")
    assert [film.tmdb_id for film in cache.load().films] == [FILM_ID]


# --- settings ---


@pytest.mark.parametrize(
    "data",
    [
        {"upcoming_territory": "XX", "upcoming_window_days": "60"},
        {"upcoming_territory": "GB", "upcoming_window_days": "45"},
        {"upcoming_territory": "GB", "upcoming_window_days": "lots"},
    ],
    ids=["territory", "window", "not-a-number"],
)
def test_settings_refuse_what_is_not_on_the_list(harness: AppHarness, data: dict) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/upcoming", data={"upcoming_enabled": "on", **data}, follow_redirects=False
    )

    assert "upcoming_invalid" in response.headers["location"]
    assert harness.client.app.state.filters.load().upcoming_enabled is False


def test_other_settings_cards_do_not_reset_it(harness: AppHarness) -> None:
    _ready(harness, territory="JP", days="180")

    harness.client.post(
        "/settings/region", data={"boxoffice_region": "GB"}, follow_redirects=False
    )

    stored = harness.client.app.state.filters.load()
    assert stored.upcoming_enabled is True
    assert stored.upcoming_territory == "JP"
    assert stored.upcoming_window_days == 180


def test_a_hand_edited_file_with_unknown_choices_still_loads(harness: AppHarness) -> None:
    harness.activate()
    path = harness.settings.config_dir / "filters.yml"
    path.write_text(
        "schema_version: 1\nupcoming_territory: XX\nupcoming_window_days: 45\n", "utf-8"
    )

    stored = harness.client.app.state.filters.load()

    assert stored.upcoming_territory == "US"
    assert stored.upcoming_window_days == 90


def test_settings_show_the_opt_in_unticked_until_it_is_chosen(harness: AppHarness) -> None:
    harness.activate()
    assert re.search(r'id="upcoming_enabled"[^>]*>', harness.client.get("/settings").text)
    tag = re.search(r'<input id="upcoming_enabled"[^>]*>', harness.client.get("/settings").text)
    assert "checked" not in tag.group(0)

    _turn_on(harness)

    tag = re.search(r'<input id="upcoming_enabled"[^>]*>', harness.client.get("/settings").text)
    assert "checked" in tag.group(0)


# --- filling the shelf ---


@respx.mock
def test_a_second_page_is_asked_for_only_if_tmdb_has_one_and_never_a_third(
    harness: AppHarness,
) -> None:
    _ready(harness)
    first = [_result(index, f"Film {index}") for index in range(1, 21)]
    second = [_result(index, f"Film {index}") for index in range(20, 41)]
    route = respx.get(DISCOVER_URL).mock(side_effect=[
        httpx.Response(200, json={"results": first, "total_pages": 5}),
        httpx.Response(200, json={"results": second, "total_pages": 5}),
    ])

    _refresh(harness)

    assert route.call_count == 2
    films = harness.client.app.state.upcoming_cache.load().films
    assert len(films) == 40  # the film on both pages is kept once


def test_the_shelf_shows_at_most_a_row_and_fills_past_what_is_left_out(
    harness: AppHarness,
) -> None:
    _ready(harness)
    _chart_with(harness, 1)
    _seed(harness, *[_film(index, f"Film Number {index}") for index in range(1, 31)])

    section = _section(harness)

    assert "Film Number 1<" not in section
    assert "Film Number 21<" in section
    assert "Film Number 22<" not in section

