"""Step 16 test: dashboard renders statuses, search, pagination, Run Now."""

from __future__ import annotations

import re

import httpx
import respx

from app.services.apps import ExternalApp
from app.services.corrections import Correction
from app.services.matcher import normalize_title
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
from app.web.dashboard import DEFAULT_LIMIT, PAGE_INCREMENT
from app.web.deps import radarr_url_for
from tests.conftest import AppHarness
from tests.integration.conftest import QUEUE_ROUTE, queue_records

RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
API = f"{RADARR_URL}/api/v3"


def _movie(rank: int, title: str, status: str) -> MovieResult:
    # normalize_title, not title.lower(): that is what the pipeline stores
    # (pipeline._reconcile), and a search test seeded with a naive lowercase would be
    # matching against data the app never actually writes.
    return MovieResult(
        rank=rank, title=title, normalized_title=normalize_title(title),
        gross_amount=rank * 1_000_000, gross_display=f"${rank}.0M", weeks_in_release=1,
        status=status, action=MovieAction.NONE, tmdb_id=1000 + rank,
        imdb_url=f"https://www.imdb.com/title/tt{rank}/",
    )


def _seed_report(harness: AppHarness, movies: list[MovieResult]) -> None:
    report = Report(
        id="report-20260812-100000-abcd", run_at="2026-08-12T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK,
        totals=ReportTotals(movies=len(movies), matched=0), movies=movies,
    )
    harness.client.app.state.reports.save(report)


def test_dashboard_renders_statuses(harness: AppHarness) -> None:
    harness.activate()
    _seed_report(
        harness,
        [
            _movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY),
            _movie(2, "Neon Rain", MovieStatus.WANTED),
            _movie(3, "The Iron Claw", MovieStatus.MISSING),
        ],
    )
    page = harness.client.get("/dashboard")
    assert page.status_code == 200
    # The dashboard is the LIBRARY view: only in-library + wanted titles appear.
    assert "Dune Part Two" in page.text
    assert "In Library" in page.text
    assert "Neon Rain" in page.text
    assert "Wanted" in page.text
    # A missing title is NOT shown here — it's added from the weekly view.
    assert "The Iron Claw" not in page.text


def test_dashboard_search_filters(harness: AppHarness) -> None:
    harness.activate()
    _seed_report(
        harness,
        [
            _movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY),
            _movie(2, "Neon Rain", MovieStatus.WANTED),
        ],
    )
    page = harness.client.get("/dashboard", params={"q": "neon"})
    assert "Neon Rain" in page.text
    assert "Dune Part Two" not in page.text


def test_dashboard_pagination(harness: AppHarness) -> None:
    """The grid scrolls, so the link only appears past the cap — not every ten titles."""
    harness.activate()
    # Library-view titles (wanted) so they show on the dashboard.
    _seed_report(
        harness,
        [_movie(i, f"Movie {i:03d}", MovieStatus.WANTED) for i in range(1, DEFAULT_LIMIT + 6)],
    )

    default_view = harness.client.get("/dashboard").text
    assert default_view.count('class="poster-title"') == DEFAULT_LIMIT
    assert f"Show {PAGE_INCREMENT} More" in default_view

    full = harness.client.get("/dashboard", params={"limit": DEFAULT_LIMIT + PAGE_INCREMENT}).text
    assert "More" not in full.split('class="load-more"')[0][-200:]  # no link left
    assert full.count('class="poster-title"') == DEFAULT_LIMIT + 5


def test_a_library_under_the_cap_shows_everything_without_a_link(
    harness: AppHarness,
) -> None:
    """The reported annoyance: a modest library was split across pages for no reason."""
    harness.activate()
    _seed_report(
        harness, [_movie(i, f"Movie {i:03d}", MovieStatus.WANTED) for i in range(1, 32)]
    )

    page = harness.client.get("/dashboard").text

    assert page.count('class="poster-title"') == 31
    assert "load-more" not in page  # nothing to click — just scroll


def test_the_load_more_label_matches_the_increment(harness: AppHarness) -> None:
    """A spelled-out number silently lies the moment the increment changes."""
    harness.activate()
    _seed_report(
        harness,
        [_movie(i, f"Movie {i:03d}", MovieStatus.WANTED) for i in range(1, DEFAULT_LIMIT + 3)],
    )

    page = harness.client.get("/dashboard").text

    assert f"Show {PAGE_INCREMENT} More" in page
    assert f"limit={DEFAULT_LIMIT + PAGE_INCREMENT}" in page


def test_empty_dashboard_prompts_setup(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/dashboard")
    assert "No box-office runs yet" in page.text


@respx.mock
def test_dashboard_drops_titles_deleted_from_radarr(harness: AppHarness) -> None:
    # A live Radarr snapshot is authoritative: a title still stored as in-library/wanted
    # but no longer in Radarr must NOT linger on the library view.
    harness.activate()
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(
        harness,
        [
            _movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY),  # tmdb 1001 — kept in Radarr
            _movie(2, "Neon Rain", MovieStatus.WANTED),          # tmdb 1002 — deleted in Radarr
        ],
    )
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 1001, "id": 5, "hasFile": True}])
    )
    page = harness.client.get("/dashboard")
    assert "Dune Part Two" in page.text  # still in Radarr -> shown
    assert "Neon Rain" not in page.text  # deleted from Radarr -> dropped


def test_run_now_triggers_pipeline(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post("/run", follow_redirects=False)
    assert response.status_code == 303
    # A run now lands on the new report's detail (the week's chart), not the list.
    assert "/reports/report-" in response.headers["location"]
    # With no Radarr configured the run still records a report (status no_app).
    assert harness.client.app.state.reports.latest() is not None


def test_library_card_shows_weeks_tracked_and_total(harness: AppHarness) -> None:
    harness.activate()
    store = harness.client.app.state.reports
    for index, week in enumerate(("2026W26", "2026W27")):
        store.save(Report(
            id=f"report-2026081{index}-100000-wk", run_at=f"2026-08-1{index}T10:00:00+00:00",
            trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
            totals=ReportTotals(movies=1, matched=1),
            movies=[_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)],
        ))

    page = harness.client.get("/dashboard").text
    assert "2 weeks tracked" in page
    assert "$2.0M tracked" in page  # $1.0M charted in each of the two weeks


# --- every connection, and where each title lives (section 14) ---

SECOND_URL = "http://radarr-4k.local:7878"
SECOND_API = f"{SECOND_URL}/api/v3"


def _two_boxes(harness: AppHarness) -> None:
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    harness.client.app.state.apps.add(name="Living Room 4K", url=SECOND_URL, api_key=RADARR_KEY)


def _chips(page: str, title: str) -> list[str]:
    """The where-chips on one card, in order, as rendered text.

    Anchored on the title ELEMENT rather than the first occurrence of the text: a card
    with a real poster also carries the title in the image's alt attribute, which sits
    before the chips and would cut the card short.
    """
    card = page.split(f'class="poster-title">{title}')[0].rsplit('class="poster-card"', 1)[1]
    return [
        chunk.split("<")[0].strip()
        for chunk in card.split('class="where-chip')[1:]
        for chunk in [chunk.partition(">")[2]]
    ]


@respx.mock
def test_a_title_on_the_second_box_appears_at_all(harness: AppHarness) -> None:
    """The bug: the page read only the primary, so anything sent elsewhere was invisible."""
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))  # not here
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 1001, "id": 5, "hasFile": True}])
    )

    page = harness.client.get("/dashboard").text
    assert "Dune Part Two" in page
    assert _chips(page, "Dune Part Two") == ["Living Room 4K"]


@respx.mock
def test_a_queued_title_says_where_it_will_land(harness: AppHarness) -> None:
    # Added but not downloaded: Wanted, and the chip names the box it is heading for.
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 1001, "id": 5, "hasFile": False}])
    )

    page = harness.client.get("/dashboard").text
    assert "Wanted" in page and "In Library" not in page
    assert _chips(page, "Dune Part Two") == ["↓ Living Room 4K"]


@respx.mock
def test_a_title_on_both_boxes_names_both(harness: AppHarness) -> None:
    """Downloaded on one, still coming on the other — one chip each, not one merged
    string. Only one copy exists, so naming its quality is still unambiguous."""
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 5, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}}}]))
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 1001, "id": 9, "hasFile": False}])
    )

    page = harness.client.get("/dashboard").text
    assert _chips(page, "Dune Part Two") == ["Main", "↓ Living Room 4K"]
    assert "In Library · Bluray-1080p" in page


@respx.mock
def test_two_downloaded_copies_claim_no_single_quality(harness: AppHarness) -> None:
    """1080p on one box and 2160p on the other — the badge must not pick one and present
    it as the title's quality."""
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 5, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}}}]))
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 9, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "WEBDL-2160p"}}}}]))

    page = harness.client.get("/dashboard").text
    assert _chips(page, "Dune Part Two") == ["Main", "Living Room 4K"]
    assert "In Library" in page
    assert "Bluray-1080p" not in page and "WEBDL-2160p" not in page


@respx.mock
def test_one_holder_still_names_the_quality(harness: AppHarness) -> None:
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 5, "hasFile": True,
         "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}}}]))
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    assert "In Library · Bluray-1080p" in harness.client.get("/dashboard").text


@respx.mock
def test_a_silent_box_cannot_evict_its_own_titles(harness: AppHarness) -> None:
    """With one connection down we cannot tell "deleted" from "lives on the box that did
    not answer", so nothing is dropped and the banner names who was silent."""
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))  # answers: absent
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(500))    # silent

    page = harness.client.get("/dashboard").text
    assert "Dune Part Two" in page  # kept — it may well live on the silent box
    assert "Couldn’t reach Living Room 4K" in page
    assert _chips(page, "Dune Part Two") == []


@respx.mock
def test_both_boxes_answering_still_drops_a_deleted_title(harness: AppHarness) -> None:
    # The authoritative case must survive the "silent box" carve-out above.
    harness.activate()
    _two_boxes(harness)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    assert "Dune Part Two" not in harness.client.get("/dashboard").text


@respx.mock
def test_a_proxy_login_page_from_radarr_does_not_500_the_dashboard(harness: AppHarness) -> None:
    """A 200 carrying HTML is how a reverse proxy in front of Radarr fails.

    It used to escape as JSONDecodeError and take the whole page down; it is an
    unreachable Radarr like any other, so the page renders with the warning banner.
    """
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, text="<html><body>Sign in to continue</body></html>")
    )

    page = harness.client.get("/dashboard")

    assert page.status_code == 200
    assert "Couldn’t reach Main" in page.text
    assert "Dune Part Two" in page.text  # stored status carries the page


# --- every title Radarr holds, not only the ones a week charted ---

POSTER_IMAGE = [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/x.jpg"}]
# posters.sized() rewrites the stored `original` to the grid width before fetching.
LIBRARY_POSTER_URL = "https://image.tmdb.org/t/p/w500/x.jpg"


def _mock_poster() -> None:
    """Radarr carries artwork on every library entry, so the grid now fetches one for
    library-only titles as well. Mocked wherever such a title is rendered."""
    respx.get(LIBRARY_POSTER_URL).mock(
        return_value=httpx.Response(200, content=b"jpeg-bytes")
    )


def _library_entry(tmdb: int, title: str, *, has_file: bool = True, year: int = 2019) -> dict:
    return {
        "tmdbId": tmdb, "id": tmdb, "title": title, "year": year,
        "hasFile": has_file, "imdbId": f"tt{tmdb}", "images": POSTER_IMAGE,
    }


@respx.mock
def test_a_library_title_no_week_ever_charted_is_still_listed(harness: AppHarness) -> None:
    """The reported bug: 30 titles in Radarr, 9 on the page.

    The grid was built purely from report history, so anything added before BoxMedia
    existed — or during a week it never scraped — was invisible, and the user concluded
    they had never added it.
    """
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])  # tmdb 1001
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        _library_entry(1001, "Dune Part Two"),
        _library_entry(5001, "The Thing"),          # never charted
        _library_entry(5002, "Alien", has_file=False),
    ]))

    page = harness.client.get("/dashboard").text

    assert "Dune Part Two" in page
    assert "The Thing" in page
    assert "Alien" in page


@respx.mock
def test_a_library_only_title_keeps_its_poster_and_shows_its_year(harness: AppHarness) -> None:
    # Radarr carries artwork on every library entry, so these are real cards, not grey
    # boxes — and the year stands in for a box-office figure we genuinely do not have.
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        _library_entry(5001, "The Thing", year=1982),
    ]))

    page = harness.client.get("/dashboard").text

    frame = page.split('class="poster-title">The Thing')[0].rsplit('class="poster-card"', 1)[1]
    assert "/posters/" in frame          # a real poster, fetched via the local cache
    assert "poster-empty" not in frame
    # The year stands where a box-office figure would be, in the same meta line.
    meta = page.split('class="poster-title">The Thing')[1].split("</div>")[1]
    assert "1982" in meta


@respx.mock
def test_a_charted_title_keeps_its_box_office_figures(harness: AppHarness) -> None:
    # The new titles must not cost the charted ones their data.
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        _library_entry(1001, "Dune Part Two"),
        _library_entry(5001, "The Thing"),
    ]))

    page = harness.client.get("/dashboard").text

    assert "$1.0M (Wk 1)" in page                       # the charted one, unchanged
    assert page.index("Dune Part Two") < page.index("The Thing")  # charted first


@respx.mock
def test_a_title_both_charted_and_in_the_library_appears_once(harness: AppHarness) -> None:
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(1001, "Dune Part Two")])
    )

    page = harness.client.get("/dashboard").text

    assert page.count('class="poster-title"') == 1


@respx.mock
def test_the_same_title_on_two_boxes_is_one_card(harness: AppHarness) -> None:
    # Deduplicated across connections, with both chips on the single card.
    harness.activate()
    _mock_poster()
    _two_boxes(harness)
    _seed_report(harness, [])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(5001, "The Thing")])
    )
    respx.get(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(5001, "The Thing")])
    )

    page = harness.client.get("/dashboard").text

    assert page.count('class="poster-title"') == 1
    assert _chips(page, "The Thing") == ["Main", "Living Room 4K"]


@respx.mock
def test_search_finds_a_library_only_title(harness: AppHarness) -> None:
    """The workflow the bug broke: knowing you own it, and being able to confirm it
    without paging back through every weekly report."""
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        _library_entry(5001, "The Thing"), _library_entry(5002, "Alien"),
    ]))

    page = harness.client.get("/dashboard", params={"q": "thing"}).text

    assert "The Thing" in page
    assert "Alien" not in page


@respx.mock
def test_the_page_states_how_many_titles_there_are(harness: AppHarness) -> None:
    # The count is what settles "I have more than this in Radarr" at a glance.
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        _library_entry(1001, "Dune Part Two"),
        _library_entry(5001, "The Thing"),
        _library_entry(5002, "Alien"),
    ]))

    assert "3 titles" in harness.client.get("/dashboard").text


@respx.mock
def test_one_title_reads_as_singular(harness: AppHarness) -> None:
    harness.activate()
    _mock_poster()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(5001, "The Thing")])
    )

    page = harness.client.get("/dashboard").text
    assert "1 title" in page and "1 titles" not in page


# --- library search folds titles the way the rest of the app does ---

SPIDER = "Spider-Man: Brand New Day"


def _seed_spider(harness: AppHarness) -> None:
    _seed_report(harness, [_movie(1, SPIDER, MovieStatus.IN_LIBRARY)])


def test_search_matches_across_punctuation(harness: AppHarness) -> None:
    """The reported asymmetry: "spider man" found the film in the weekly search and not
    in the library one, because this box compared raw substrings."""
    harness.activate()
    _seed_spider(harness)

    page = harness.client.get("/dashboard", params={"q": "spider man"}).text

    assert SPIDER in page


def test_search_still_rejects_a_title_that_is_not_there(harness: AppHarness) -> None:
    harness.activate()
    _seed_spider(harness)

    page = harness.client.get("/dashboard", params={"q": "zzz"}).text

    assert SPIDER not in page


def test_search_folds_articles_the_way_the_matcher_does(harness: AppHarness) -> None:
    # "The Odyssey" is stored as "odyssey"; both spellings of the query must find it.
    harness.activate()
    _seed_report(harness, [_movie(1, "The Odyssey", MovieStatus.IN_LIBRARY)])

    for query in ("the odyssey", "odyssey"):
        assert "The Odyssey" in harness.client.get("/dashboard", params={"q": query}).text


def test_search_folds_numerals_the_way_the_matcher_does(harness: AppHarness) -> None:
    harness.activate()
    _seed_report(harness, [_movie(1, "Toy Story II", MovieStatus.IN_LIBRARY)])

    assert "Toy Story II" in harness.client.get("/dashboard", params={"q": "toy story 2"}).text


def test_an_empty_query_still_lists_the_whole_library(harness: AppHarness) -> None:
    """The guard that separates this box from the weekly search: filtering a listing with
    nothing to filter on shows the listing, it does not empty it."""
    harness.activate()
    _seed_spider(harness)

    assert SPIDER in harness.client.get("/dashboard").text
    assert SPIDER in harness.client.get("/dashboard", params={"q": ""}).text
    assert SPIDER in harness.client.get("/dashboard", params={"q": "   "}).text


def test_a_query_that_is_only_an_article_narrows_nothing(harness: AppHarness) -> None:
    """`normalize_title("the")` is empty — the matcher drops articles. A query carrying
    no searchable token cannot narrow anything, so the library stays listed rather than
    coming back empty at someone mid-way through typing a title."""
    harness.activate()
    _seed_spider(harness)

    assert SPIDER in harness.client.get("/dashboard", params={"q": "the"}).text


@respx.mock
def test_search_covers_titles_radarr_holds_that_no_report_charted(
    harness: AppHarness,
) -> None:
    """The library-only cards build their own normalized_title (dashboard.py), so they
    have to fold the same way as the charted ones or half the grid would be unsearchable.
    """
    harness.activate()
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 4242, "title": "WALL·E", "year": 2008, "hasFile": True, "id": 7},
    ]))

    page = harness.client.get("/dashboard", params={"q": "wall e"}).text

    assert "WALL" in page


# --- tracked is not lifetime (F9) ---


def _tracked_weeks(
    harness: AppHarness, totals: list[int | None], *, run_at: list[str] | None = None
) -> None:
    """One report per week, each carrying the running total Mojo reported that week."""
    store = harness.client.app.state.reports
    for index, total in enumerate(totals):
        movie = _movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)
        store.save(Report(
            id=f"report-2026081{index}-100000-wk",
            run_at=(run_at[index] if run_at else f"2026-08-1{index}T10:00:00+00:00"),
            trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=f"2026W2{6 + index}",
            totals=ReportTotals(movies=1, matched=1),
            movies=[movie.model_copy(update={"total_gross": total})],
        ))


def test_a_card_names_the_tracked_sum_and_the_lifetime_gross(harness: AppHarness) -> None:
    """The reported defect: two weeks of a nine-week run summed to $2.0M and were labelled
    "total", for a film that had actually taken $473.0M."""
    harness.activate()
    _tracked_weeks(harness, [460_000_000, 473_000_000])

    page = harness.client.get("/dashboard").text

    assert "2 weeks tracked" in page
    assert "$2.0M tracked" in page
    assert "$473.0M lifetime" in page
    assert "$2.0M total" not in page, "the unlabelled total is what was misleading"


def test_a_card_with_no_stored_lifetime_shows_only_what_it_tracked(
    harness: AppHarness,
) -> None:
    """Every report written before the scraper read Mojo's Total Gross column. The tracked
    figure is still true, so it stays; an invented lifetime would not be."""
    harness.activate()
    _tracked_weeks(harness, [None, None])

    page = harness.client.get("/dashboard").text

    assert "$2.0M tracked" in page
    assert "lifetime" not in page


def test_a_rerun_of_an_older_week_cannot_shrink_the_lifetime(harness: AppHarness) -> None:
    """The trap, and why this is a max rather than the newest sighting.

    `list_reports` orders by when a report was WRITTEN, and re-running an older week is
    how a week's figures get better — so the oldest week can legitimately be the newest
    report. Reading the running total from the front of that list would replace $473.0M
    with the $460.0M the film had reached weeks earlier.
    """
    harness.activate()
    # W26 carries the smaller, earlier total but was written LAST.
    _tracked_weeks(
        harness,
        [460_000_000, 473_000_000],
        run_at=["2026-08-20T10:00:00+00:00", "2026-08-11T10:00:00+00:00"],
    )

    page = harness.client.get("/dashboard").text

    assert "$473.0M lifetime" in page
    assert "$460.0M" not in page


@respx.mock
def test_a_library_title_no_week_charted_shows_neither_figure(harness: AppHarness) -> None:
    """The extras have no chart data at all — a lifetime there would be fabricated, and a
    tracked sum would be a confident $0.0M."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _mock_poster()
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_library_entry(4242, "Old Favourite")])
    )

    page = harness.client.get("/dashboard").text

    assert "Old Favourite" in page
    assert "tracked" not in page
    assert "lifetime" not in page


# --- the chips reach the Radarr they name (F11) ---

SLUG = "dune-part-two-1001"


def _held_entry(tmdb: int = 1001, *, slug: str | None = SLUG, has_file: bool = True) -> dict:
    entry = {"tmdbId": tmdb, "id": tmdb, "title": "Dune Part Two", "year": 2024,
             "hasFile": has_file, "imdbId": f"tt{tmdb}", "images": []}
    if slug is not None:
        entry["titleSlug"] = slug
    return entry


@respx.mock
def test_a_chip_links_to_that_film_on_that_radarr(harness: AppHarness) -> None:
    """The dead end this closes: you read "Main", opened Radarr yourself, and searched
    for the film again. `titleSlug` is what Radarr's own UI routes on, so the address is
    the one that instance uses rather than a shape guessed from outside."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[_held_entry()]))

    page = harness.client.get("/dashboard").text

    assert f'href="{RADARR_URL}/movie/{SLUG}"' in page
    assert 'rel="noopener noreferrer"' in page
    assert 'target="_blank"' in page


@respx.mock
def test_the_link_is_the_stored_address_not_the_one_we_were_asked_on(
    harness: AppHarness,
) -> None:
    """The chip href is a connection address out of apps.yml. Building it from anything
    the request carried would let a spoofed Host point every chip at another site."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[_held_entry()]))

    page = harness.client.get(
        "/dashboard",
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
    ).text

    assert f'href="{RADARR_URL}/movie/{SLUG}"' in page
    assert "evil.example" not in page


@respx.mock
def test_a_radarr_that_names_no_route_leaves_the_chip_as_text(harness: AppHarness) -> None:
    """Nothing to link to is not a reason to invent a link — a guessed URL would land on
    "cannot find movie", which is worse than the plain chip it replaced."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_held_entry(slug=None)])
    )

    page = harness.client.get("/dashboard").text

    assert _chips(page, "Dune Part Two") == ["Main"]  # the chip is still there
    assert "/movie/" not in page.split('class="where"')[1].split("</div>")[0]


@respx.mock
def test_a_pending_chip_links_too(harness: AppHarness) -> None:
    """A queued title is exactly the one worth opening in Radarr — the queue is the thing
    BoxMedia does not show."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.WANTED)])
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[_held_entry(has_file=False)])
    )

    page = harness.client.get("/dashboard").text

    assert "where-chip-pending" in page
    assert f'href="{RADARR_URL}/movie/{SLUG}"' in page


@respx.mock
def test_the_chips_are_not_nested_inside_the_posters_own_link(
    harness: AppHarness,
) -> None:
    """The structural trap. `.where` used to live inside the poster's anchor, and an
    anchor nested in an anchor is not something a browser tolerates: the parser closes the
    outer one early and ejects everything after it, which would leave the chips absolutely
    positioned against the page instead of the card. Asserted on the rendered markup
    because no assertion about the chips themselves would notice.
    """
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[_held_entry()]))

    page = harness.client.get("/dashboard").text
    poster_anchor = page.split('class="poster-frame', 1)[1].split("</a>", 1)[0]

    assert "where-chip" not in poster_anchor
    assert "<a" not in poster_anchor, "the poster's anchor must contain no other link"
    # ...and the chips are still positioned, i.e. the shell went in with them.
    assert 'class="poster-shell"' in page


def test_an_address_a_browser_would_read_differently_yields_no_link() -> None:
    """`normalize_url` prepends http:// to anything without a scheme, so no stored address
    can be a `javascript:` URL — but it does pass an embedded tab or newline through, and a
    browser strips those before parsing. That gap is where smuggling lives, and ExternalApp
    is a dataclass that re-validates nothing on read, so the address goes through the same
    guard the film's own website link does before it becomes an href."""
    movie = RadarrMovie(tmdb_id=1001, title="Dune Part Two", year=2024, has_file=True,
                        title_slug=SLUG)
    honest = ExternalApp(id="app-x", name="Main", url=RADARR_URL, api_key_encrypted="x")
    assert radarr_url_for(honest, movie) == f"{RADARR_URL}/movie/{SLUG}"

    for smuggled in ("http://radarr\t.local:7878", "http://radarr.local:7878/\r\nX: y"):
        app = ExternalApp(id="app-x", name="Main", url=smuggled, api_key_encrypted="x")
        assert radarr_url_for(app, movie) is None, smuggled


def test_a_slug_cannot_escape_the_movie_path() -> None:
    """The slug is Radarr's own value, but it lands in a URL path — percent-encoded so a
    stray slash names a film rather than a different endpoint on that instance."""
    app = ExternalApp(id="app-x", name="Main", url=RADARR_URL, api_key_encrypted="x")
    movie = RadarrMovie(tmdb_id=1, title="x", year=None, has_file=True,
                        title_slug="../../system/tasks")

    assert radarr_url_for(app, movie) == f"{RADARR_URL}/movie/..%2F..%2Fsystem%2Ftasks"


# --- a Wanted chip says how far along the download is (F13) ---


def _fill_classes(page: str) -> list[str]:
    """The progress-fill classes in a rendered page. Matched with the trailing digit —
    `where-chip-pending` contains `where-chip-p`, so a bare substring check passes
    vacuously and would have proved nothing."""
    return re.findall(r"where-chip-p\d+", page)


def _wanted_on_main(harness: AppHarness, *, radarr_id: int = 77) -> None:
    """One title added to the primary and still downloading."""
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.WANTED)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": radarr_id, "title": "Dune Part Two", "year": 2024,
         "hasFile": False, "images": [], "titleSlug": "dune-part-two-1001"},
    ]))


@respx.mock
def test_a_downloading_chip_carries_a_progress_fill(harness: AppHarness) -> None:
    """The defect: a stalled download and one at 99% rendered identically, so "Wanted"
    meant either "no release found in months" or "arriving in four minutes"."""
    harness.activate()
    _wanted_on_main(harness)
    # 580MB left of 1GB = 42%, which draws in the nearest ten-percent step.
    queue_records([{"movieId": 77, "size": 1_000_000_000, "sizeleft": 580_000_000}])

    page = harness.client.get("/dashboard").text

    assert "where-chip-p40" in page
    assert "42% downloaded" in page  # the exact figure, on the chip as a tooltip


@respx.mock
def test_an_empty_queue_renders_exactly_as_before(harness: AppHarness) -> None:
    """Nothing downloading is the normal case for most of a library, and it must not gain
    markup."""
    harness.activate()
    _wanted_on_main(harness)
    queue_records([])

    page = harness.client.get("/dashboard").text

    assert "where-chip-pending" in page  # still a pending chip
    assert not _fill_classes(page)
    assert "downloaded" not in page


@respx.mock
def test_a_queue_that_cannot_be_read_renders_exactly_as_before(harness: AppHarness) -> None:
    """"Could not look" is not "nothing is downloading". A connection whose queue failed
    must render the chip it always did rather than claim 0%."""
    harness.activate()
    _wanted_on_main(harness)
    respx.routes[QUEUE_ROUTE].mock(side_effect=httpx.ConnectError("queue down"))

    page = harness.client.get("/dashboard").text

    assert "where-chip-pending" in page
    assert not _fill_classes(page)


@respx.mock
def test_a_downloaded_title_is_never_asked_about(harness: AppHarness) -> None:
    """A film that already has its file is not queued for that file, so a stale queue
    record must not put a fill on a finished chip."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _seed_report(harness, [_movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 77, "title": "Dune Part Two", "year": 2024,
         "hasFile": True, "images": []},
    ]))
    queue_records([{"movieId": 77, "size": 100, "sizeleft": 50}])

    page = harness.client.get("/dashboard").text

    assert not _fill_classes(page)


@respx.mock
def test_progress_reads_the_queue_inside_the_libraries_gather(harness: AppHarness) -> None:
    """The acceptance criterion: no new sequential await. Both reads are launched together,
    so a page with a queue costs the slowest single request, not the sum of two rounds.

    Asserted on the source, because a timing assertion on two mocked calls would measure
    nothing.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent.parent / "app" / "web"
              / "dashboard.py").read_text(encoding="utf-8")

    gather = source.split("await asyncio.gather(")[1].split("\n    )")[0]
    assert "load_all_radarr_libraries(request)" in gather
    assert "load_all_radarr_queues(request)" in gather


# --- sums never add two currencies together (M2) ---


def _week_in(harness: AppHarness, week: str, gross: int, currency: str, *,
             total: int | None = None, run_at: str | None = None) -> None:
    """One stored week for the same film, fetched in a given currency."""
    movie = _movie(1, "Dune Part Two", MovieStatus.IN_LIBRARY)
    harness.client.app.state.reports.save(Report(
        id=f"report-{week}-{'usd' if currency == '$' else 'gbp'}",
        run_at=run_at or f"2026-08-1{week[-1]}T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week, currency=currency,
        region="" if currency == "$" else "GB",
        totals=ReportTotals(movies=1, matched=1),
        movies=[movie.model_copy(update={
            "gross_amount": gross, "gross_display": f"{currency}{gross}", "total_gross": total,
        })],
    ))


@respx.mock
def test_a_tracked_sum_never_adds_two_currencies(harness: AppHarness) -> None:
    """The number this feature exists for. Two dollar weeks and one pound week: the sum
    is the dollars, because adding pounds to them would not be a formatting slip — it
    would be a number that means nothing, printed with the confidence of one that does.
    """
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _week_in(harness, "2026W30", 1_000_000, "$")
    _week_in(harness, "2026W31", 2_000_000, "$")
    _week_in(harness, "2026W32", 9_000_000, "£", run_at="2026-08-09T10:00:00+00:00")
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 7, "title": "Dune Part Two", "year": 2024,
         "hasFile": True, "images": []},
    ]))

    page = harness.client.get("/dashboard").text

    assert "3 weeks tracked" in page       # the history is still three weeks long
    assert "$3.0M tracked" in page         # ...and the money is only the dollars
    assert "£" not in page.split("tracked")[0][-200:]


@respx.mock
def test_an_all_pound_history_reads_in_pounds(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    _week_in(harness, "2026W30", 1_000_000, "£", total=40_000_000)
    _week_in(harness, "2026W31", 2_000_000, "£", total=42_000_000)
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 7, "title": "Dune Part Two", "year": 2024,
         "hasFile": True, "images": []},
    ]))

    page = harness.client.get("/dashboard").text

    assert "£3.0M tracked" in page
    assert "£42.0M lifetime" in page
    assert "$" not in page


@respx.mock
def test_a_lifetime_figure_is_never_folded_across_currencies(harness: AppHarness) -> None:
    """`max` across currencies is not a bigger number, it is a meaningless one — a £40M
    total must not out-rank a $80M one just because 40 < 80."""
    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=RADARR_URL, api_key=RADARR_KEY)
    # The freshest report is the dollar one, so the card speaks dollars.
    _week_in(harness, "2026W30", 1_000_000, "£", total=999_000_000,
             run_at="2026-08-01T10:00:00+00:00")
    _week_in(harness, "2026W31", 2_000_000, "$", total=80_000_000,
             run_at="2026-08-20T10:00:00+00:00")
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 1001, "id": 7, "title": "Dune Part Two", "year": 2024,
         "hasFile": True, "images": []},
    ]))

    page = harness.client.get("/dashboard").text

    assert "$80.0M lifetime" in page   # not the larger pound figure
    assert "999" not in page
    assert "$2.0M tracked" in page     # ...and only the dollar week is summed


def test_a_corrected_film_stays_one_film_across_its_weeks(harness: AppHarness) -> None:
    """The dashboard folds a film's weeks together by the chart title it charted under.

    Correcting one week used to re-key that week to the corrected title, and the film
    split into two cards — one showing three weeks under Mojo's spelling and one showing a
    single week under Radarr's. Which is why having the same title in several weeks made
    the problem worse rather than better.
    """
    harness.activate()
    for index, week in enumerate(("2026W30", "2026W31", "2026W32")):
        harness.client.app.state.reports.save(Report(
            id=f"report-2026080{index + 1}-100000-corr", week=week,
            run_at=f"2026-08-0{index + 1}T10:00:00+00:00",
            trigger=RunTrigger.SCHEDULED, status=RunStatus.OK,
            totals=ReportTotals(movies=1, matched=1),
            movies=[MovieResult(
                rank=5, title="Miroirs No. 3", normalized_title="miroirs no 3",
                gross_amount=1_000_000, gross_display="$1.0M", weeks_in_release=index + 1,
                status=MovieStatus.WANTED, action=MovieAction.NONE, tmdb_id=999,
            )],
        ))

    harness.client.app.state.reports.apply_correction(
        "miroirs no 3", Correction(tmdb_id=111, title="Mirrors No. 3", year=2025)
    )

    page = harness.client.get("/dashboard").text
    assert page.count("Mirrors No. 3") >= 1
    assert "Miroirs No. 3" not in page, "the chart's spelling is still on a card"
    assert "3 weeks" in page, "the film's weeks were split across two cards"
