"""Per-title add: quality dropdown from Radarr + add-to-Radarr route."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from app.services.radarr_options import RadarrOptions, RadarrOptionsCache, fetch_options
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

RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
API = f"{RADARR_URL}/api/v3"


class _FakeRadarr:
    async def quality_profiles(self):
        return [(4, "HD-1080p"), (5, "Ultra-HD")]

    async def root_folders(self):
        return ["/movies", "/movies/4k"]


async def test_fetch_options_maps() -> None:
    options = await fetch_options(_FakeRadarr())
    assert options.profiles[0].id == 4
    assert options.profiles[0].name == "HD-1080p"
    assert options.root_folders == ["/movies", "/movies/4k"]
    assert options.profile_name(5) == "Ultra-HD"


def test_options_cache_round_trip(tmp_path: Path) -> None:
    cache = RadarrOptionsCache(tmp_path)
    assert cache.load("app-1").is_empty()
    cache.save(
        "app-1",
        RadarrOptions.model_validate(
            {"profiles": [{"id": 4, "name": "HD-1080p"}], "root_folders": ["/movies"]}
        ),
    )
    loaded = cache.load("app-1")
    assert loaded.profiles[0].name == "HD-1080p"
    assert loaded.root_folders == ["/movies"]


def test_options_cache_keeps_connections_apart(tmp_path: Path) -> None:
    """Radarr assigns profile ids per database — id 4 is HD-1080p on one box and
    something else on another, so one shared list would offer the wrong quality."""
    cache = RadarrOptionsCache(tmp_path)
    cache.save("app-local", RadarrOptions.model_validate(
        {"profiles": [{"id": 4, "name": "HD-1080p"}], "root_folders": ["/movies"]}))
    cache.save("app-remote", RadarrOptions.model_validate(
        {"profiles": [{"id": 4, "name": "Ultra-HD"}], "root_folders": ["/mnt/4k"]}))

    assert cache.load("app-local").profiles[0].name == "HD-1080p"
    assert cache.load("app-remote").profiles[0].name == "Ultra-HD"
    assert cache.load("app-local").root_folders == ["/movies"]
    assert cache.load("app-remote").root_folders == ["/mnt/4k"]


def test_forgetting_a_connection_drops_only_its_entry(tmp_path: Path) -> None:
    cache = RadarrOptionsCache(tmp_path)
    cache.save("app-a", RadarrOptions.model_validate({"root_folders": ["/a"]}))
    cache.save("app-b", RadarrOptions.model_validate({"root_folders": ["/b"]}))

    cache.forget("app-a")

    assert cache.load("app-a").is_empty()
    assert cache.load("app-b").root_folders == ["/b"]


def test_a_v1_cache_file_is_discarded_rather_than_misread(tmp_path: Path) -> None:
    """v1 stored one un-keyed blob. Reading it as any connection's options would hand a
    1080p box the 4K box's profile ids. It is a cache, so dropping it costs one refetch."""
    from app.core import filestore

    filestore.write_yaml(
        tmp_path / "radarr_options.yml",
        {"profiles": [{"id": 4, "name": "HD-1080p"}], "root_folders": ["/movies"]},
        schema_version=1,
    )
    assert RadarrOptionsCache(tmp_path).load("app-1").is_empty()


def _configure(
    harness: AppHarness, *, quality_profile_id: int = 4, root_folder: str = "/movies"
) -> str:
    """One connection, carrying the quality and folder it adds at.

    Both live on the connection now — there is no global pair to set and no per-title
    field to send, so this is the only place an add can get them from.
    """
    app = harness.client.app.state.apps.add(
        name="Radarr", url=RADARR_URL, api_key=RADARR_KEY
    )
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=quality_profile_id, root_folder=root_folder
    )
    return app.id


@respx.mock
def test_add_movie_uses_the_connection_profile(harness: AppHarness) -> None:
    # The connection's own quality is what goes out — nothing is chosen per title.
    _mock_options([{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}], ["/movies"])
    # The add route then checks the library (for duplicate prevention).
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/movie").mock(
        return_value=httpx.Response(
            201, json={"tmdbId": 558449, "title": "Gladiator II", "year": 2024}
        )
    )
    harness.activate()
    _configure(harness, quality_profile_id=5)

    response = harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II", "year": "2024"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "status=added" in response.headers["location"]
    sent = json.loads(route.calls.last.request.content)
    assert sent["tmdbId"] == 558449
    assert sent["qualityProfileId"] == 5  # from Settings, with no form field involved
    assert sent["rootFolderPath"] == "/movies"
    assert "movie_added_manual" in "\n".join(harness.audit_lines())


@respx.mock
def test_a_submitted_quality_profile_is_ignored(harness: AppHarness) -> None:
    """The field is gone from the route, so a crafted POST cannot re-introduce it.

    Sending a profile id the connection does offer is the strongest form of this: if the
    route read the form at all, 5 would go out instead of the configured 4.
    """
    _mock_options([{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}], ["/movies"])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _configure(harness, quality_profile_id=4)

    harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II", "quality_profile_id": "5"},
        follow_redirects=False,
    )
    assert json.loads(route.calls.last.request.content)["qualityProfileId"] == 4


@respx.mock
def test_the_card_asks_for_nothing_but_the_connection(harness: AppHarness) -> None:
    """No quality and no folder control on the card — the whole point of the change.

    Asserted on the rendered page rather than the route, because "removed from the UI but
    still read" was exactly what was asked against.
    """
    _mock_options([{"id": 4, "name": "HD-1080p"}], ["/movies", "/movies/4k"])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    _configure(harness)
    _seed_missing_title(harness)

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert "Add to Radarr" in page
    assert "Add to Radarr as" not in page
    assert 'name="root_folder"' not in page
    # The upgrade form still owns a quality select; the add form must not have one.
    add_form = page.split('action="/add-movie"')[1].split("</form>")[0]
    assert "select" not in add_form


@respx.mock
def test_add_movie_refuses_duplicate(harness: AppHarness) -> None:
    _mock_options([{"id": 4, "name": "HD-1080p"}], ["/movies"])
    # Radarr already has this title -> no duplicate add.
    respx.get(f"{API}/movie").mock(
        return_value=httpx.Response(200, json=[{"tmdbId": 558449, "id": 7, "hasFile": True}])
    )
    add_route = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _configure(harness)
    response = harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II"},
        follow_redirects=False,
    )
    assert "status=already_in_radarr" in response.headers["location"]
    assert not add_route.called  # never attempted the add


@respx.mock
def test_upgrade_movie_sets_profile_and_searches(harness: AppHarness) -> None:
    respx.get(f"{API}/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "qualityProfileId": 4, "monitored": True})
    )
    put_route = respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={"id": 7}))
    command = respx.post(f"{API}/command").mock(return_value=httpx.Response(201, json={}))
    # Profile 5 is vetted against this connection's own list before it is sent.
    _mock_options([{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}], ["/movies"])
    harness.activate()
    _configure(harness)
    response = harness.client.post(
        "/upgrade-movie",
        data={"radarr_id": "7", "quality_profile_id": "5"},
        follow_redirects=False,
    )
    assert "status=upgraded" in response.headers["location"]
    import json as _json
    assert _json.loads(put_route.calls.last.request.content)["qualityProfileId"] == 5
    assert command.called


@respx.mock
def test_upgrade_reports_upgraded_despite_failed_search(harness: AppHarness) -> None:
    # The profile PUT succeeds but the search command fails: the user must still see
    # "upgraded", not "add_failed" — the quality profile WAS changed.
    respx.get(f"{API}/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "qualityProfileId": 4, "monitored": True})
    )
    respx.put(f"{API}/movie/7").mock(return_value=httpx.Response(200, json={"id": 7}))
    respx.post(f"{API}/command").mock(return_value=httpx.Response(500))
    _mock_options([{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}], ["/movies"])
    harness.activate()
    _configure(harness)
    response = harness.client.post(
        "/upgrade-movie",
        data={"radarr_id": "7", "quality_profile_id": "5"},
        follow_redirects=False,
    )
    assert "status=upgraded" in response.headers["location"]


def test_add_movie_without_config_is_guarded(harness: AppHarness) -> None:
    harness.activate()  # no Radarr, no root folder
    response = harness.client.post(
        "/add-movie",
        data={"tmdb_id": "1", "title": "X", "year": "2024"},
        follow_redirects=False,
    )
    assert "status=add_config" in response.headers["location"]


@respx.mock
def test_options_cache_not_rewritten_when_unchanged(harness: AppHarness) -> None:
    # Identical Radarr options across two page loads must be written once, not on every
    # view (the redundant write churns radarr_options.yml under the global write lock).
    respx.get(f"{API}/system/status").mock(return_value=httpx.Response(200, json={"version": "5"}))
    respx.get(f"{API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    harness.activate()
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)

    cache = harness.client.app.state.radarr_options
    saves = {"count": 0}
    original_save = cache.save

    def counting_save(app_id: str, options: object) -> None:
        saves["count"] += 1
        original_save(app_id, options)

    cache.save = counting_save

    harness.client.get("/settings")
    harness.client.get("/settings")
    assert saves["count"] == 1  # written on the first load, skipped on the identical second


SECOND_RADARR_URL = "http://radarr-4k.local:7878"
SECOND_API = f"{SECOND_RADARR_URL}/api/v3"


@respx.mock
def test_add_movie_uses_the_primary_connection(harness: AppHarness) -> None:
    """With two connections configured, adds must go to the one marked primary — not
    whichever happens to be first in the file."""
    _mock_options([{"id": 4, "name": "HD-1080p"}], ["/movies"])
    respx.get(f"{SECOND_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{SECOND_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}])
    )
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    first_add = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={}))
    respx.get(f"{SECOND_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    second_add = respx.post(f"{SECOND_API}/movie").mock(
        return_value=httpx.Response(201, json={"tmdbId": 558449, "title": "Gladiator II"})
    )
    harness.activate()
    _configure(harness)  # adds the first connection + defaults
    second = harness.client.app.state.apps.add(
        name="Radarr 4K", url=SECOND_RADARR_URL, api_key=RADARR_KEY
    )

    harness.client.post(f"/settings/apps/{second.id}/primary", follow_redirects=False)
    response = harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II"},
        follow_redirects=False,
    )

    assert "status=added" in response.headers["location"]
    assert second_add.called  # the chosen Radarr got the movie
    assert not first_add.called  # and the other one did not


def test_primary_is_shown_and_switchable_in_settings(harness: AppHarness) -> None:
    harness.activate()
    first = harness.client.app.state.apps.add(
        name="Radarr", url=RADARR_URL, api_key=RADARR_KEY
    )
    second = harness.client.app.state.apps.add(
        name="Radarr 4K", url=SECOND_RADARR_URL, api_key=RADARR_KEY
    )

    page = harness.client.get("/settings").text
    assert "Primary" in page  # the first is primary by default
    assert f"/settings/apps/{second.id}/primary" in page  # the other offers the switch

    harness.client.post(f"/settings/apps/{second.id}/primary", follow_redirects=False)
    assert harness.client.app.state.apps.primary_id() == second.id
    assert f"/settings/apps/{first.id}/primary" in harness.client.get("/settings").text


# --- the root folder is the connection's, never the title's ---

REPORT_ID = "report-20260814-120000-fldr"


def _mock_options(profiles: list[dict], folders: list[str]) -> None:
    respx.get(f"{API}/qualityprofile").mock(return_value=httpx.Response(200, json=profiles))
    respx.get(f"{API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": path} for path in folders])
    )


def _seed_missing_title(harness: AppHarness) -> None:
    movie = MovieResult(
        rank=1, title="Gladiator II", normalized_title="gladiator 2", gross_amount=1,
        gross_display="$0.0M", weeks_in_release=1, status=MovieStatus.MISSING,
        action=MovieAction.NONE, tmdb_id=558449,
    )
    harness.client.app.state.reports.save(Report(
        id=REPORT_ID, run_at="2026-08-14T12:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK,
        totals=ReportTotals(movies=1, matched=0), movies=[movie],
    ))


@respx.mock
def test_add_movie_uses_the_connection_folder(harness: AppHarness) -> None:
    # Two folders on the box, and the add still takes the configured one without asking.
    _mock_options([{"id": 4, "name": "HD-1080p"}], ["/movies", "/movies/4k"])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _configure(harness, root_folder="/movies/4k")

    harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II"},
        follow_redirects=False,
    )
    assert json.loads(route.calls.last.request.content)["rootFolderPath"] == "/movies/4k"


@respx.mock
def test_a_submitted_root_folder_never_reaches_radarr(harness: AppHarness) -> None:
    """An unvetted path would tell Radarr to build a library anywhere on its filesystem.

    Removing the field is what makes that impossible; this pins it, because a re-added
    parameter would send /etc straight through.
    """
    _mock_options([{"id": 4, "name": "HD-1080p"}], ["/movies", "/movies/4k"])
    respx.get(f"{API}/movie").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/movie").mock(return_value=httpx.Response(201, json={}))
    harness.activate()
    _configure(harness)

    harness.client.post(
        "/add-movie",
        data={"tmdb_id": "558449", "title": "Gladiator II", "root_folder": "/etc"},
        follow_redirects=False,
    )
    assert json.loads(route.calls.last.request.content)["rootFolderPath"] == "/movies"
