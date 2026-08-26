"""TV step 14 integration test: the calendar page.

The claim that matters most is the same one Discover makes and for the same reason: a
render of a FRESH cache reaches the network for nothing. Every test here that renders
without seeding a stale cache does so with no respx mock, so a page that started fetching
on every open would fail on an unmocked call rather than quietly becoming a page that
goes down when somebody's NAS does.

The second claim is the week nav. It offers exactly what the cache can answer, clamps
anything typed by hand into that range, and never links to a week that would render as an
empty grid — because an empty grid reads as "nothing is on", which is a different claim
from "we never fetched that far".
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import respx

from app.services.calendar import (
    CALENDAR_CACHE_FILENAME,
    KIND_MOVIE,
    KIND_SERIES,
    STATE_DOWNLOADED,
    STATE_DOWNLOADING,
    STATE_MISSING,
    STATE_MONITORED,
    STATE_TODAY,
    CalendarEntry,
)
from app.web.calendar import NEVER_FETCHED
from tests.conftest import AppHarness

SONARR_URL = "http://127.0.0.1:2"
SONARR_KEY = "fedcba9876543210fedcba9876543210"
RADARR_URL = "http://127.0.0.1:3"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
SONARR_CALENDAR = f"{SONARR_URL}/api/v3/calendar"
RADARR_CALENDAR = f"{RADARR_URL}/api/v3/calendar"

SONARR_NAME = "Attic Sonarr"
RADARR_NAME = "Attic Radarr"

NOW = datetime.now(UTC)
TODAY = NOW.date()
MONDAY = TODAY - timedelta(days=TODAY.weekday())
SUNDAY = MONDAY + timedelta(days=6)
SATURDAY = MONDAY + timedelta(days=5)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _episode(
    *,
    title: str = "The Hollow Coast",
    day: date | None = None,
    hour: int = 21,
    state: str = STATE_MONITORED,
    progress: float | None = None,
    connection: str = SONARR_NAME,
) -> CalendarEntry:
    return CalendarEntry(
        kind=KIND_SERIES, title=title, sub="S02E07 · 21:00",
        when=_at(day or MONDAY, hour), state=state, connection=connection,
        tvdb_id=121361, progress=progress,
    )


def _film(
    *,
    title: str = "Paper Comet",
    day: date | None = None,
    state: str = STATE_DOWNLOADED,
    connection: str = RADARR_NAME,
) -> CalendarEntry:
    return CalendarEntry(
        kind=KIND_MOVIE, title=title, sub="Physical release",
        when=_at(day or MONDAY, 9), state=state, connection=connection, tmdb_id=550,
    )


def _seed(harness: AppHarness, *entries: CalendarEntry) -> None:
    harness.client.app.state.calendar_cache.save(list(entries))


def _stale(harness: AppHarness) -> None:
    """Age the cache past its TTL so the next open refreshes it, without waiting."""
    cache_dir = harness.settings.cache_dir
    path = cache_dir / CALENDAR_CACHE_FILENAME
    import json

    document = json.loads(path.read_text("utf-8")) if path.exists() else {
        "schema_version": 1, "entries": []
    }
    document["fetched_at"] = time.time() - 10_000
    path.write_text(json.dumps(document), "utf-8")


def _sonarr(harness: AppHarness) -> str:
    harness.client.app.state.apps.add(
        name=SONARR_NAME, url=SONARR_URL, api_key=SONARR_KEY, kind="sonarr"
    )
    return harness.client.app.state.apps.list_apps("sonarr")[0].id


def _radarr(harness: AppHarness) -> str:
    harness.client.app.state.apps.add(
        name=RADARR_NAME, url=RADARR_URL, api_key=RADARR_KEY, kind="radarr"
    )
    return harness.client.app.state.apps.list_apps("radarr")[0].id


def _entries_in(page: str) -> list[str]:
    """Every entry title the grid rendered, in order."""
    return re.findall(r'<span class="entry-title" title="([^"]*)"', page)


_DAY_START = re.compile(r'<div class="day(?: day-today)?">')


def _day_columns(page: str) -> list[str]:
    """The seven day columns' markup, so a test can look inside one of them.

    Sliced on the column openings rather than matched with a lazy group: `.day-head` also
    starts with `day`, so anything simpler ends a column at its own heading.
    """
    week = page.split('<div class="week">', 1)[1].split('<div class="legend">', 1)[0]
    starts = [match.start() for match in _DAY_START.finditer(week)]
    assert len(starts) == 7, f"expected seven columns, found {len(starts)}"
    ends = [*starts[1:], len(week)]
    return [week[begin:end] for begin, end in zip(starts, ends, strict=True)]


# --- the grid ---


def test_a_seeded_cache_renders_the_week(harness: AppHarness) -> None:
    """No respx mock: a render that reached the network would fail here on an unmocked
    call, which is exactly the guarantee being asserted."""
    harness.activate()
    _seed(harness, _episode(), _film(day=MONDAY + timedelta(days=1)))

    page = harness.client.get("/calendar").text

    assert "The Hollow Coast" in page
    assert "Paper Comet" in page
    assert page.count('class="day') >= 7


def test_a_film_and_an_episode_share_a_day_and_the_film_reads_differently(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed(harness, _episode(day=MONDAY), _film(day=MONDAY))

    monday = _day_columns(harness.client.get("/calendar").text)[0]

    assert "The Hollow Coast" in monday
    assert "Paper Comet" in monday
    # The one visual difference the mockup asks for, and only on the film.
    assert monday.count("entry-film") == 1


def test_each_state_carries_its_own_class_and_its_own_words(
    harness: AppHarness,
) -> None:
    """Colour is never the only signal — every entry says its state in text too."""
    harness.activate()
    _seed(
        harness,
        _episode(title="Grabbed", day=MONDAY, state=STATE_DOWNLOADED),
        _episode(title="Coming in", day=MONDAY, hour=22, state=STATE_DOWNLOADING,
                 progress=61.5),
        _episode(title="Not here", day=MONDAY, hour=8, state=STATE_MISSING),
        _episode(title="Tonight", day=MONDAY, hour=23, state=STATE_TODAY),
        _episode(title="Later", day=MONDAY, hour=7, state=STATE_MONITORED),
    )

    page = harness.client.get("/calendar").text

    assert "entry-ok" in page and "Downloaded" in page
    assert "↓ 62%" in page
    assert "entry-warn" in page and "Aired — no file yet" in page
    assert "entry-now" in page and "Airs today" in page
    assert "entry-idle" in page and "Monitored" in page


def test_an_empty_saturday_keeps_its_column(harness: AppHarness) -> None:
    """A day with nothing on it is a fact, not a broken column: it keeps its head, its
    date and its border, and says so in words."""
    harness.activate()
    _seed(harness, _episode(day=MONDAY))

    columns = _day_columns(harness.client.get("/calendar").text)

    saturday = columns[5]
    assert f"{SATURDAY.day}/{SATURDAY.month}" in saturday
    assert "Nothing" in saturday
    assert "entry-title" not in saturday


def test_today_is_the_only_column_in_the_primary_border(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(day=TODAY))

    page = harness.client.get("/calendar").text

    assert page.count("day day-today") == 1
    assert "· Today" in page


def test_dates_are_day_first_never_american(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(day=SUNDAY))

    page = harness.client.get("/calendar").text

    assert f"{SUNDAY.day}/{SUNDAY.month}" in page
    assert f"{SUNDAY.month}/{SUNDAY.day}" not in page or SUNDAY.day == SUNDAY.month


# --- the chips ---


def test_the_movies_chip_narrows_to_films(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(), _film())

    page = harness.client.get("/calendar?type=movies").text

    assert _entries_in(page) == ["Paper Comet"]


def test_the_tv_chip_narrows_to_series(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(), _film())

    page = harness.client.get("/calendar?type=tv").text

    assert _entries_in(page) == ["The Hollow Coast"]


def test_an_unknown_chip_shows_everything(harness: AppHarness) -> None:
    """Read-tolerant: a bookmarked or hand-edited `?type=` shows the widest view rather
    than an error page."""
    harness.activate()
    _seed(harness, _episode(), _film())

    page = harness.client.get("/calendar?type=cassettes").text

    assert sorted(_entries_in(page)) == ["Paper Comet", "The Hollow Coast"]


def test_a_chip_keeps_the_week_you_are_looking_at(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode())
    last_week = (MONDAY - timedelta(weeks=1)).isoformat()

    page = harness.client.get(f"/calendar?week={last_week}").text

    assert f"?type=movies&amp;week={last_week}" in page


# --- the week nav ---


def test_the_nav_reaches_two_weeks_either_side(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode())

    page = harness.client.get("/calendar").text

    assert (MONDAY - timedelta(weeks=1)).isoformat() in page
    assert (MONDAY + timedelta(weeks=1)).isoformat() in page


def test_the_nav_stops_at_the_edge_of_what_the_cache_holds(
    harness: AppHarness,
) -> None:
    """No link to a week the cache cannot answer — it would render an empty grid, which
    reads as "nothing is on"."""
    harness.activate()
    _seed(harness, _episode())
    earliest = (MONDAY - timedelta(weeks=2)).isoformat()

    page = harness.client.get(f"/calendar?week={earliest}").text

    assert (MONDAY - timedelta(weeks=3)).isoformat() not in page
    assert (MONDAY - timedelta(weeks=1)).isoformat() in page  # forward still offered


def test_the_nav_stops_at_the_far_edge_too(harness: AppHarness) -> None:
    """The mirror of the test above. Asserted separately because one guard passing says
    nothing about the other — they are two expressions, not one."""
    harness.activate()
    _seed(harness, _episode())
    latest = (MONDAY + timedelta(weeks=2)).isoformat()

    page = harness.client.get(f"/calendar?week={latest}").text

    assert (MONDAY + timedelta(weeks=3)).isoformat() not in page
    assert (MONDAY + timedelta(weeks=1)).isoformat() in page  # back still offered


def test_a_week_far_outside_the_window_clamps_rather_than_emptying(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed(harness, _episode())

    page = harness.client.get("/calendar?week=2031-04-07").text

    latest = MONDAY + timedelta(weeks=2)
    assert f"{latest.day}/{latest.month}" in page


def test_a_week_that_is_not_a_date_shows_this_week(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(day=MONDAY))

    page = harness.client.get("/calendar?week=last-tuesday-ish").text

    assert "The Hollow Coast" in page
    assert "day day-today" in page


def test_any_day_of_a_week_resolves_to_that_whole_week(harness: AppHarness) -> None:
    """The window is anchored on the week, so a link built from a Thursday shows the same
    seven days as one built from its Monday."""
    harness.activate()
    _seed(harness, _episode(day=MONDAY - timedelta(weeks=1)))
    last_monday = MONDAY - timedelta(weeks=1)

    from_monday = harness.client.get(f"/calendar?week={last_monday}").text
    from_thursday = harness.client.get(
        f"/calendar?week={last_monday + timedelta(days=3)}"
    ).text

    assert _entries_in(from_monday) == _entries_in(from_thursday) == ["The Hollow Coast"]


# --- the fortnight list ---


def test_the_list_is_anchored_on_today_not_on_the_week_on_screen(
    harness: AppHarness,
) -> None:
    """What is coming does not change when you look back at last week."""
    harness.activate()
    _seed(harness, _episode(title="Soon", day=TODAY + timedelta(days=3)))
    last_week = (MONDAY - timedelta(weeks=1)).isoformat()

    page = harness.client.get(f"/calendar?week={last_week}").text

    assert "soon-title" in page
    assert page.count("Soon") >= 1


def test_the_list_leaves_out_what_has_already_gone(harness: AppHarness) -> None:
    harness.activate()
    _seed(
        harness,
        _episode(title="Last fortnight", day=TODAY - timedelta(days=10)),
        _episode(title="Next week", day=TODAY + timedelta(days=6)),
    )

    listed = re.search(
        r"The next 14 days.*", harness.client.get("/calendar").text, re.S
    ).group(0)

    assert "Next week" in listed
    assert "Last fortnight" not in listed


def test_something_on_today_is_named_today_rather_than_dated(
    harness: AppHarness,
) -> None:
    """A date beside the word "today" is a date the reader has to check against a
    calendar to understand — on the one row where they already know the answer."""
    harness.activate()
    _seed(harness, _episode(title="Tonight's one", day=TODAY, hour=23))

    listed = re.search(
        r"The next 14 days.*", harness.client.get("/calendar").text, re.S
    ).group(0)

    assert "Today 23:00" in listed
    assert f"{TODAY.day}/{TODAY.month} 23:00" not in listed


def test_the_list_stops_at_a_fortnight_even_though_the_cache_reaches_further(
    harness: AppHarness,
) -> None:
    """The cache covers the week plus a fortnight either side, so there is real data past
    the horizon — the list is a choice about what a person can act on, not a side effect
    of what happens to be cached."""
    harness.activate()
    _seed(
        harness,
        _episode(title="Within reach", day=TODAY + timedelta(days=13)),
        _episode(title="Beyond the horizon", day=TODAY + timedelta(days=20)),
    )

    listed = re.search(
        r"The next 14 days.*", harness.client.get("/calendar").text, re.S
    ).group(0)

    assert "Within reach" in listed
    assert "Beyond the horizon" not in listed


def test_an_empty_fortnight_says_so(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness, _episode(title="Long gone", day=TODAY - timedelta(days=12)))

    page = harness.client.get("/calendar").text

    assert "Nothing due in the next 14 days." in page


# --- the header line ---


@respx.mock
def test_a_first_open_against_a_dead_server_says_never(harness: AppHarness) -> None:
    """"—" reads as a time we do not know; this is a fetch that has NOT HAPPENED, which is
    the state the line exists to make visible — and the state a first open against a box
    that is switched off lands in."""
    harness.activate()
    _sonarr(harness)
    respx.get(url__startswith=SONARR_CALENDAR).mock(
        return_value=httpx.Response(503, json={})
    )
    respx.get(url__regex=rf"{SONARR_URL}/api/v3/queue").mock(
        return_value=httpx.Response(503, json={})
    )

    page = harness.client.get("/calendar").text

    assert f"Last fetch: {NEVER_FETCHED}" in page
    assert "Some connections did not answer" in page


def test_the_header_states_the_refresh_that_actually_happens(
    harness: AppHarness,
) -> None:
    """No morning job exists yet (step 15), so the line states the rule that is true
    today rather than promising a time nothing keeps."""
    harness.activate()
    _seed(harness, _episode())

    page = harness.client.get("/calendar").text

    assert "Last fetch:" in page
    assert "Next automatic fetch" not in page
    assert "more than fifteen minutes old" in page


def test_the_header_names_the_next_automatic_fetch_once_one_is_scheduled(
    harness: AppHarness,
) -> None:
    """Step 15's morning job is what fills this in. A bare TestClient never runs the
    lifespan, so the app under test has no scheduler until one is put there."""
    harness.activate()
    _seed(harness, _episode())
    harness.client.app.state.scheduler = SimpleNamespace(
        next_calendar_run_at=lambda: datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
    )
    try:
        page = harness.client.get("/calendar").text
    finally:
        harness.client.app.state.scheduler = None

    assert "Next automatic fetch: 27/8/2026 06:00" in page
    assert "more than fifteen minutes old" not in page


def test_an_install_with_no_connections_is_told_what_to_do(
    harness: AppHarness,
) -> None:
    harness.activate()

    page = harness.client.get("/calendar").text

    assert "No Sonarr or Radarr connection yet" in page
    assert "/settings" in page


# --- refreshing on open ---


@respx.mock
def test_a_stale_cache_is_refreshed_on_open(harness: AppHarness) -> None:
    harness.activate()
    _sonarr(harness)
    _seed(harness, _episode(title="Yesterday's answer", day=MONDAY))
    _stale(harness)
    respx.get(url__startswith=SONARR_CALENDAR).mock(
        return_value=httpx.Response(200, json=[{
            "id": 5, "seriesId": 14, "seasonNumber": 2, "episodeNumber": 7,
            "title": "Low Tide", "airDateUtc": _at(MONDAY, 21).isoformat(),
            "hasFile": False, "monitored": True,
            "series": {"title": "The Hollow Coast", "tvdbId": 121361},
        }])
    )
    respx.get(url__regex=rf"{SONARR_URL}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": []})
    )

    page = harness.client.get("/calendar").text

    assert "The Hollow Coast" in page
    assert "Yesterday's answer" not in page


@respx.mock
def test_a_connection_that_does_not_answer_keeps_its_rows_and_says_so(
    harness: AppHarness,
) -> None:
    """The page must never turn "we could not look" into "nothing is due"."""
    harness.activate()
    _sonarr(harness)
    _radarr(harness)
    _seed(harness, _episode(day=MONDAY), _film(day=MONDAY))
    _stale(harness)
    respx.get(url__startswith=SONARR_CALENDAR).mock(
        return_value=httpx.Response(200, json=[{
            "id": 5, "seriesId": 14, "seasonNumber": 2, "episodeNumber": 7,
            "title": "Low Tide", "airDateUtc": _at(MONDAY, 21).isoformat(),
            "hasFile": False, "monitored": True,
            "series": {"title": "The Hollow Coast", "tvdbId": 121361},
        }])
    )
    respx.get(url__regex=rf"{SONARR_URL}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    respx.get(url__startswith=RADARR_CALENDAR).mock(
        return_value=httpx.Response(500, json={})
    )

    page = harness.client.get("/calendar").text

    assert "Some connections did not answer" in page
    assert "Paper Comet" in page  # the Radarr's last known rows are still on the week


def test_a_fresh_cache_is_not_refetched(harness: AppHarness) -> None:
    """No respx mock and a Sonarr configured: a page that refreshed regardless of the TTL
    would fail here on an unmocked call."""
    harness.activate()
    _sonarr(harness)
    _seed(harness, _episode())

    assert harness.client.get("/calendar").status_code == 200


# --- the gate ---


def test_the_page_needs_a_session(harness: AppHarness) -> None:
    response = harness.client.get("/calendar", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]
