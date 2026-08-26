"""TV step 16 integration test: the unified spine, and the promise it had to keep.

Two claims carry this step.

The first is a **regression contract**, not a feature: ruling 4 says the Movies chip on
the merged Library renders exactly what the dashboard rendered before the merge. That is
checkable only because the old render was captured first — `golden_movies_grid.html` was
committed against the pre-merge page, before a line of the rewrite was written — and the
test below diffs the new page's grid against it.

The second is that nothing lost its way: `/dashboard` still resolves, every page still
marks the right nav entry, and the progress poller still finds the film chips it drives
while leaving the new series chips alone, because their band means something else.
"""

from __future__ import annotations

import re

import httpx
import respx

from app.services.apps import ExternalApp
from app.services.mediaserver import MediaServerFetch, MediaServerSeries
from app.services.posters import SERIES_POSTER_WIDTH
from app.services.series import SERIES_CACHE_FILENAME
from app.services.series import _cached_from as _cached
from app.services.sonarr import SonarrSeries
from app.web.library import _sonarr_url_for
from tests.conftest import AppHarness
from tests.integration.golden import (
    GOLDEN_PATH,
    grid_of,
    mock_radarr,
    seed_library,
)

SONARR_URL = "http://sonarr.local:8989"
SONARR_KEY = "fedcba9876543210fedcba9876543210"
SONARR_NAME = "Attic Sonarr"


def _series(
    *,
    sonarr_id: int = 14,
    tvdb_id: int = 121361,
    title: str = "The Hollow Coast",
    year: int | None = 2023,
    episode_count: int = 34,
    episode_file_count: int = 34,
    tmdb_id: int | None = 1396,
    title_slug: str | None = "the-hollow-coast",
    poster_url: str | None = None,
) -> SonarrSeries:
    return SonarrSeries(
        sonarr_id=sonarr_id, tvdb_id=tvdb_id, title=title, year=year,
        monitored=True, ended=False,
        episode_count=episode_count, episode_file_count=episode_file_count,
        poster_url=poster_url, path=f"/tv/{title_slug}",
        imdb_id=None, tmdb_id=tmdb_id, title_slug=title_slug,
    )


def _seed_sonarr(harness: AppHarness, *series: SonarrSeries) -> str:
    """A Sonarr connection with its snapshot already on disk, so the page needs no
    live read — which is also what keeps these tests off the network."""
    harness.client.app.state.apps.add(
        name=SONARR_NAME, url=SONARR_URL, api_key=SONARR_KEY, kind="sonarr"
    )
    app_id = harness.client.app.state.apps.list_apps("sonarr")[0].id
    harness.client.app.state.series_cache.save(app_id, series or (_series(),))
    return app_id


def _seed_media_server(harness: AppHarness) -> None:
    """A Plex that holds the seeded series, snapshotted so nothing goes near a socket."""
    harness.client.post(
        "/settings/media-server",
        data={"url": "http://plex.local:32400", "token": "t" * 20, "kind": "plex"},
        follow_redirects=False,
    )
    harness.client.app.state.media_server_cache.save(MediaServerFetch(
        movies=(),
        truncated=False,
        series=(MediaServerSeries(title="The Hollow Coast", year=2023, tvdb_id=121361),),
    ))


def _titles_in(page: str) -> list[str]:
    return re.findall(r'<div class="poster-title">([^<]*)</div>', page)


# --- ruling 4: the Movies chip is what the dashboard was ---


@respx.mock
def test_the_movies_chip_renders_the_pre_merge_grid(harness: AppHarness) -> None:
    """The golden diff. Captured against the old page before the rewrite existed, so a
    card that changed shape, lost a chip, or reordered fails here rather than being
    noticed by whoever opens their library next week."""
    harness.activate()
    seed_library(harness)
    mock_radarr()

    page = harness.client.get("/library?type=movies")

    assert page.status_code == 200
    assert grid_of(page.text) == GOLDEN_PATH.read_text(encoding="utf-8").rstrip("\n")


@respx.mock
def test_the_all_chip_still_holds_every_film(harness: AppHarness) -> None:
    """Same films, different order — All sorts by title because nothing merges the two
    kinds' orders honestly."""
    harness.activate()
    seed_library(harness)
    mock_radarr()

    page = harness.client.get("/library")

    assert sorted(_titles_in(page.text)) == [
        "Harbour Lights", "Neon Rain", "Paper Comet", "The Salt Line"
    ]
    assert _titles_in(page.text) == sorted(_titles_in(page.text), key=str.casefold)


# --- the nav ---


def test_the_spine_offers_five_destinations(harness: AppHarness) -> None:
    harness.activate()

    nav = re.search(
        r'<nav class="navlinks">(.*?)</nav>', harness.client.get("/library").text, re.S
    ).group(1)

    assert re.findall(r">([^<]+)</a>", nav) == [
        "Library", "Box Office", "Discover", "Calendar", "Settings"
    ]
    for path in ("/library", "/reports", "/discover", "/calendar", "/settings"):
        assert f'href="{path}"' in nav


def test_every_page_marks_its_own_nav_entry(harness: AppHarness) -> None:
    """One active entry per page, and the right one — the assertion the rewrite could
    most easily have got subtly wrong on one page out of six."""
    harness.activate()
    for path, expected in (
        ("/library", "Library"),
        ("/reports", "Box Office"),
        ("/discover", "Discover"),
        ("/calendar", "Calendar"),
        ("/settings", "Settings"),
        ("/security", "Settings"),
    ):
        page = harness.client.get(path)
        assert page.status_code == 200, path
        active = re.findall(r'class="active">([^<]+)</a>', page.text)
        assert active == [expected], f"{path} marked {active}"


def test_the_brand_goes_to_the_library(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/library").text

    assert '<a class="brand" href="/library">' in page


# --- the old address ---


def test_dashboard_permanently_redirects_to_library(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/library"


def test_the_redirect_keeps_the_query_it_was_given(harness: AppHarness) -> None:
    """A bookmarked search, or a proxy rule carrying one, must not be silently emptied."""
    harness.activate()

    response = harness.client.get("/dashboard?q=neon&type=movies", follow_redirects=False)

    assert response.headers["location"] == "/library?q=neon&type=movies"


def test_the_old_address_is_still_behind_the_session_gate(harness: AppHarness) -> None:
    """The redirect must not become a way to find out whether a path exists without
    signing in — the middleware gate answers first, exactly as it did before."""
    response = harness.client.get("/dashboard", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_signing_in_lands_on_the_library(harness: AppHarness) -> None:
    """Where a session begins. The first-run wizard's own detour to Settings is
    unchanged and has its own test — this install already has a connection."""
    password = harness.activate()
    harness.client.app.state.apps.add(
        name="Attic Radarr", url="http://radarr.local:7878", api_key="0" * 32
    )
    harness.client.post("/logout", follow_redirects=False)

    response = harness.client.post(
        "/login", data={"username": "admin", "password": password},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/library"


# --- the series half ---


def test_a_series_card_says_what_it_holds(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=34, episode_file_count=26))

    page = harness.client.get("/library?type=tv").text

    assert "The Hollow Coast" in page
    assert "Missing 8 episodes" in page
    assert '<div class="rank-chip">TV</div>' in page
    assert SONARR_NAME in page


def test_a_complete_series_says_so_instead(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=34, episode_file_count=34))

    page = harness.client.get("/library?type=tv").text

    assert "Complete — 34 episodes" in page
    assert "Missing" not in page


def test_one_missing_episode_is_not_pluralised(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=34, episode_file_count=33))

    assert "Missing 1 episode<" in harness.client.get("/library?type=tv").text


def test_the_completeness_band_is_the_share_on_disk(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=10, episode_file_count=6))

    page = harness.client.get("/library?type=tv").text

    assert "where-chip-p60" in page
    assert "where-chip-pending" in page


def test_a_complete_series_fills_its_band(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=10, episode_file_count=10))

    page = harness.client.get("/library?type=tv").text

    assert "where-chip-p100" in page
    assert "where-chip-pending" not in page


def test_a_series_with_no_episodes_gets_no_band(harness: AppHarness) -> None:
    """None, not zero: an empty band would say "you have none of this", and "we do not
    know how long this is" is a different thing."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=0, episode_file_count=0))

    page = harness.client.get("/library?type=tv").text

    # Not `"where-chip-p" not in page` — that is a prefix of `where-chip-pending`, which
    # an incomplete series legitimately carries.
    assert not re.search(r"where-chip-p\d", page)


def test_the_series_chip_links_into_sonarr(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(title_slug="the-hollow-coast"))

    page = harness.client.get("/library?type=tv").text

    assert f'href="{SONARR_URL}/series/the-hollow-coast"' in page


def test_a_series_without_a_slug_gets_a_plain_chip(harness: AppHarness) -> None:
    """A guessed address is worse than none — the same rule the film chip follows."""
    harness.activate()
    _seed_sonarr(harness, _series(title_slug=None))

    page = harness.client.get("/library?type=tv").text

    assert f"{SONARR_URL}/series/" not in page
    assert SONARR_NAME in page


def test_a_series_card_opens_its_detail_when_there_is_an_id_to_open(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed_sonarr(harness, _series(tmdb_id=1396))

    page = harness.client.get("/library?type=tv").text

    assert 'href="/shows/1396" data-show="1396"' in page


def test_a_series_with_no_tmdb_id_gets_no_link(harness: AppHarness) -> None:
    """Sonarr keys on TVDB and only sometimes carries a TMDB id; the detail route needs
    one, so a card without it is plain rather than broken."""
    harness.activate()
    _seed_sonarr(harness, _series(tmdb_id=None))

    page = harness.client.get("/library?type=tv").text

    assert "poster-frame-plain" in page
    assert "data-show=" not in page


def test_the_same_series_on_two_connections_is_one_card(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.apps.add(
        name=SONARR_NAME, url=SONARR_URL, api_key=SONARR_KEY, kind="sonarr"
    )
    harness.client.app.state.apps.add(
        name="Loft Sonarr", url="http://sonarr2.local:8989", api_key=SONARR_KEY,
        kind="sonarr",
    )
    for app in harness.client.app.state.apps.list_apps("sonarr"):
        harness.client.app.state.series_cache.save(app.id, (_series(),))

    page = harness.client.get("/library?type=tv").text

    assert _titles_in(page) == ["The Hollow Coast"]


def test_a_removed_connections_snapshot_is_not_rendered(harness: AppHarness) -> None:
    """The snapshot file outlives the connection until something prunes it; a card for a
    Sonarr that no longer exists would offer a link to nowhere."""
    harness.activate()
    app_id = _seed_sonarr(harness)
    harness.client.app.state.apps.remove(app_id)

    page = harness.client.get("/library?type=tv").text

    assert "The Hollow Coast" not in page


def test_an_unreadable_snapshot_costs_the_tv_half_and_not_the_page(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed_sonarr(harness)
    (harness.settings.cache_dir / SERIES_CACHE_FILENAME).write_text("{ nope", "utf-8")

    page = harness.client.get("/library")

    assert page.status_code == 200
    assert "The Hollow Coast" not in page.text


# --- the chips ---


@respx.mock
def test_the_chips_count_each_kind(harness: AppHarness) -> None:
    harness.activate()
    seed_library(harness)
    mock_radarr()
    _seed_sonarr(harness, _series(), _series(sonarr_id=9, tvdb_id=9, title="Low Orbit"))

    page = harness.client.get("/library").text

    counts = re.findall(r'<span class="chip-count">(\d+)</span>', page)
    assert counts == ["6", "4", "2"]


@respx.mock
def test_a_chip_narrows_the_grid(harness: AppHarness) -> None:
    harness.activate()
    seed_library(harness)
    mock_radarr()
    _seed_sonarr(harness)

    assert "The Hollow Coast" not in harness.client.get("/library?type=movies").text
    assert "Neon Rain" not in harness.client.get("/library?type=tv").text
    assert "The Hollow Coast" in harness.client.get("/library").text


def test_an_unknown_chip_shows_everything(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness)

    page = harness.client.get("/library?type=cassettes").text

    assert "The Hollow Coast" in page


# --- search across both ---


@respx.mock
def test_search_reaches_both_kinds(harness: AppHarness) -> None:
    harness.activate()
    seed_library(harness)
    mock_radarr()
    _seed_sonarr(harness, _series(title="The Salt Line Chronicles", tvdb_id=77, tmdb_id=77))

    page = harness.client.get("/library?q=salt+line").text

    assert "The Salt Line" in page
    assert "The Salt Line Chronicles" in page
    assert "Neon Rain" not in page


@respx.mock
def test_the_counts_follow_the_search(harness: AppHarness) -> None:
    """With a query in the box the chips answer "how many of each kind matched", which is
    what someone about to press one of them wants to know."""
    harness.activate()
    seed_library(harness)
    mock_radarr()
    _seed_sonarr(harness)

    page = harness.client.get("/library?q=neon").text

    assert re.findall(r'<span class="chip-count">(\d+)</span>', page) == ["1", "1", "0"]


def test_the_search_keeps_the_chip_you_are_on(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness)

    page = harness.client.get("/library?type=tv").text

    assert '<input type="hidden" name="type" value="tv">' in page


def test_a_search_that_matches_nothing_says_so(harness: AppHarness) -> None:
    harness.activate()
    _seed_sonarr(harness)

    page = harness.client.get("/library?q=nothing-like-this").text

    assert "Nothing here matches" in page


# --- the progress poller ---


@respx.mock
def test_the_poller_still_finds_every_film_chip(harness: AppHarness) -> None:
    harness.activate()
    seed_library(harness)
    mock_radarr()
    _seed_sonarr(harness)

    page = harness.client.get("/library").text

    polled = re.findall(r'data-progress="([^"]+)"', page)
    assert len(polled) == 2  # the two films still downloading
    assert all(":" in key for key in polled)


@respx.mock
def test_a_series_completeness_band_is_never_polled(harness: AppHarness) -> None:
    """The poller answers in Radarr download percentages and repaints any `where-chip` it
    is pointed at. A series band means how much you HAVE, so it carries no key — otherwise
    the poller would quietly replace one claim with the other."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=10, episode_file_count=6))

    grid = harness.client.get("/library?type=tv").text.split('<div class="poster-grid">')[1]

    assert "where-chip-p60" in grid
    assert "data-progress" not in grid


# --- the page still behaves like the page it replaced ---


@respx.mock
def test_an_unreachable_radarr_still_says_so(harness: AppHarness) -> None:
    harness.activate()
    seed_library(harness)
    respx.get(re.compile(r".*/api/v3/movie")).mock(side_effect=httpx.ConnectError("down"))

    page = harness.client.get("/library").text

    assert "Couldn’t reach" in page


def test_a_library_with_nothing_in_it_points_somewhere(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/library").text

    assert "No box-office runs yet" in page
    assert "/reports" in page


# --- the gaps the mutation pass found ---


def test_a_band_that_does_not_land_on_a_step_is_rounded_to_one(
    harness: AppHarness,
) -> None:
    """7 of 9 is 77.8%, and there is no `where-chip-p78` — a per-card width would have to
    be an inline style and the CSP forbids one, so the band is drawn in tenths."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=9, episode_file_count=7))

    page = harness.client.get("/library?type=tv").text

    assert "where-chip-p80" in page


def test_more_files_than_episodes_still_fills_the_band_and_no_more(
    harness: AppHarness,
) -> None:
    """Sonarr counts specials in some totals and not others, so a library really can
    report 12 files for 10 episodes. `where-chip-p120` is not a class that exists, so an
    uncapped step would silently render no band at all on a complete series."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=10, episode_file_count=12))

    page = harness.client.get("/library?type=tv").text

    assert "where-chip-p100" in page
    assert not re.search(r"where-chip-p1[1-9]\d", page)


def test_a_hand_edited_connection_address_never_becomes_an_href() -> None:
    """The layer `safe_external_url` exists for. `apps.yml` is a file a person can edit,
    and a scheme that arrived that way skips the validation that runs on save — the CSP
    would refuse to EXECUTE a javascript: href, and this stops it being rendered as a
    link in the first place.
    """
    hostile = ExternalApp(
        id="app-hand-edited", name=SONARR_NAME,
        url="javascript://evil.example/%0aalert(1)",
        api_key_encrypted="x", kind="sonarr",
    )

    assert _sonarr_url_for(hostile, _cached(_series())) is None


def test_the_save_path_neutralises_the_same_address_before_it_is_stored(
    harness: AppHarness,
) -> None:
    """Belt and braces, and worth pinning: `normalize_url` does not recognise the scheme,
    so it prepends http:// and the stored value is inert rather than rejected."""
    harness.activate()
    harness.client.app.state.apps.add(
        name=SONARR_NAME, url="javascript://evil.example/%0aalert(1)",
        api_key=SONARR_KEY, kind="sonarr",
    )
    app_id = harness.client.app.state.apps.list_apps("sonarr")[0].id
    harness.client.app.state.series_cache.save(app_id, (_series(),))

    page = harness.client.get("/library?type=tv").text

    assert 'href="javascript:' not in page
    assert "The Hollow Coast" in page


def test_a_complete_series_gets_no_media_server_hint(harness: AppHarness) -> None:
    """Only where it changes what you would do. A complete series is complete, and
    "your Plex also has it" adds nothing to that."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=34, episode_file_count=34))
    _seed_media_server(harness)

    page = harness.client.get("/library?type=tv").text

    assert "Complete — 34 episodes" in page
    assert "Already in" not in page


def test_an_incomplete_series_does_get_one(harness: AppHarness) -> None:
    """The mirror, so the test above cannot pass by the hint never appearing at all: the
    episodes you are missing may already be watchable."""
    harness.activate()
    _seed_sonarr(harness, _series(episode_count=34, episode_file_count=26))
    _seed_media_server(harness)

    page = harness.client.get("/library?type=tv").text

    assert "Already in Plex" in page


@respx.mock
def test_a_series_poster_is_fetched_at_the_series_width(harness: AppHarness) -> None:
    """One width per medium, or the cache holds two copies of every image and `prune`
    deletes whichever the page did not ask for."""
    harness.activate()
    _seed_sonarr(harness, _series(poster_url="https://image.tmdb.org/t/p/original/x.jpg"))
    route = respx.get(url__startswith="https://image.tmdb.org").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff" + b"0" * 900)
    )

    harness.client.get("/library?type=tv")

    assert route.called
    assert f"/{SERIES_POSTER_WIDTH}/" in str(route.calls[0].request.url)
