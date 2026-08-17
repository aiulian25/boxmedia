"""Sending a title to a chosen Radarr connection (design option C).

The browser picks WHICH configured connection; it never says what that connection points
at. Quality and folder are resolved against that instance's own options, because Radarr
assigns profile ids per database and root folders are paths on that host.
"""

from __future__ import annotations

import json

import httpx
import respx

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
from tests.integration.conftest import queue_records

LOCAL_URL = "http://local.radarr:7878"
REMOTE_URL = "http://remote.radarr:7878"
LOCAL_API = f"{LOCAL_URL}/api/v3"
REMOTE_API = f"{REMOTE_URL}/api/v3"
KEY = "0123456789abcdef0123456789abcdef"
TMDB = 558449
REPORT_ID = "report-20260816-120000-tgt1"


def _menu_entries(page: str) -> dict[str, bool]:
    """Each Add-menu entry as {connection name: can it be clicked}."""
    menu = page.split('class="target-menu"')[1].split("</div>")[0]
    entries = {}
    for chunk in menu.split("<button")[1:]:
        attributes, _, body = chunk.partition(">")
        name = body.split('class="target-name">')[1].split("<")[0]
        entries[name] = "disabled" not in attributes
    return entries


def _seed(harness: AppHarness) -> None:
    movie = MovieResult(
        rank=1, title="Gladiator II", normalized_title="gladiator 2",
        gross_amount=45_000_000, gross_display="$45.0M", weeks_in_release=1,
        status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=TMDB, year=2024,
    )
    harness.client.app.state.reports.save(Report(
        id=REPORT_ID, run_at="2026-08-16T12:00:00+00:00", trigger=RunTrigger.MANUAL,
        status=RunStatus.OK, week="2026W27", totals=ReportTotals(movies=1, matched=0),
        movies=[movie],
    ))


def _two_instances(harness: AppHarness) -> tuple[str, str]:
    """A 1080p local box and a 4K remote one — the operator's actual setup."""
    local = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    remote = harness.client.app.state.apps.add(name="4K", url=REMOTE_URL, api_key=KEY)
    respx.get(f"{LOCAL_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{LOCAL_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    # Same id 4, a DIFFERENT profile — the collision this design exists to avoid.
    respx.get(f"{REMOTE_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "Ultra-HD"}]))
    respx.get(f"{REMOTE_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/mnt/4k"}]))
    harness.client.app.state.apps.set_defaults(
        remote.id, quality_profile_id=4, root_folder="/mnt/4k")
    return local.id, remote.id


@respx.mock
def test_a_title_can_be_sent_to_a_chosen_connection(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness)
    _local, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    local_add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))
    remote_add = respx.post(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(201, json={}))

    harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II",
              "target": remote_id},
        follow_redirects=False,
    )

    assert remote_add.called and not local_add.called
    sent = json.loads(remote_add.calls.last.request.content)
    assert sent["rootFolderPath"] == "/mnt/4k"  # the remote box's folder, not /movies


@respx.mock
def test_an_empty_target_still_means_the_primary(harness: AppHarness) -> None:
    # Every pre-existing form posts no target; that must keep working untouched.
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    local_add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))

    harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II"},
        follow_redirects=False,
    )
    assert local_add.called


@respx.mock
def test_an_unknown_target_is_refused_not_trusted(harness: AppHarness) -> None:
    """The security boundary: the form picks which connection, never a new one."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    local_add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))
    remote_add = respx.post(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(201, json={}))

    response = harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II",
              "target": "app-does-not-exist"},
        follow_redirects=False,
    )

    assert "status=add_config" in response.headers["location"]
    assert not local_add.called and not remote_add.called


@respx.mock
def test_the_target_decides_the_quality_not_the_form(harness: AppHarness) -> None:
    """Profile ids are per-database, so the form does not get to name one at all.

    Picking the remote is the whole instruction: it adds at ITS Ultra-HD, and the
    submitted id is dead weight the route never reads.
    """
    harness.activate()
    _seed(harness)
    _local, remote_id = _two_instances(harness)
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    remote_add = respx.post(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(201, json={}))

    harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II",
              "target": remote_id, "quality_profile_id": "99"},  # not on the remote box
        follow_redirects=False,
    )

    sent = json.loads(remote_add.calls.last.request.content)
    assert sent["qualityProfileId"] == 4  # the remote's own default, not 99


@respx.mock
def test_the_card_says_which_connection_already_has_it(harness: AppHarness) -> None:
    """Without this a film sent to the 4K box reads "Missing" on the card forever."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(
        200, json=[{"tmdbId": TMDB, "id": 9, "hasFile": True, "title": "Gladiator II"}]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text
    assert "In Library · 4K" in page
    assert "Missing" not in page


@respx.mock
def test_the_menu_lists_connections_by_name_and_nothing_else(harness: AppHarness) -> None:
    """You pick a box, not a quality — so the menu carries names, and the button is the
    same "Add to Radarr" it is with one connection."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text
    assert "Add to Radarr" in page
    assert ">Main<" in page and ">4K<" in page
    assert "Ultra-HD" not in page   # the quality is settled in Settings, not on the card
    assert "/mnt/4k" not in page


@respx.mock
def test_the_primary_heads_the_menu(harness: AppHarness) -> None:
    # The plain button adds to the primary, so the primary is the first thing its caret
    # offers — otherwise the button's target is the one entry you cannot see.
    harness.activate()
    _seed(harness)
    _local, remote_id = _two_instances(harness)
    harness.client.post(f"/settings/apps/{remote_id}/primary", follow_redirects=False)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    menu = harness.client.get(f"/reports/{REPORT_ID}").text.split('class="target-menu"')[1]
    assert menu.index(">4K<") < menu.index(">Main<")


@respx.mock
def test_a_dead_primary_no_longer_hides_the_working_one(harness: AppHarness) -> None:
    """The card used to need the primary's quality list to render at all, so one
    unreachable box removed the Add control from every title — including for the box that
    was up. Nothing is asked per title now, so the menu still offers the healthy one."""
    harness.activate()
    _seed(harness)
    local = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    remote = harness.client.app.state.apps.add(name="4K", url=REMOTE_URL, api_key=KEY)
    respx.get(f"{LOCAL_API}/qualityprofile").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/rootfolder").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(500))
    respx.get(f"{REMOTE_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "Ultra-HD"}]))
    respx.get(f"{REMOTE_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/mnt/4k"}]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    harness.client.app.state.apps.set_defaults(
        remote.id, quality_profile_id=4, root_folder="/mnt/4k")

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert _menu_entries(page) == {"Main": False, "4K": True}  # listed, one usable
    assert f'value="{local.id}"' in page and f'value="{remote.id}"' in page


@respx.mock
def test_a_single_connection_keeps_the_plain_button(harness: AppHarness) -> None:
    # No caret when there is nowhere else to send to.
    harness.activate()
    _seed(harness)
    harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    respx.get(f"{LOCAL_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{LOCAL_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text
    assert "Add to Radarr" in page
    assert "split-add" not in page


@respx.mock
def test_per_connection_defaults_are_vetted_against_that_instance(
    harness: AppHarness,
) -> None:
    """Settings must not store a profile id the connection does not offer."""
    harness.activate()
    _local, remote_id = _two_instances(harness)
    # The Settings page also probes each connection's health.
    respx.get(f"{LOCAL_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    respx.get(f"{REMOTE_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    harness.client.get("/settings")  # populates each connection's options cache

    response = harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "4K", "url": REMOTE_URL, "api_key": "",
              "quality_profile_id": "77", "root_folder": "/mnt/4k"},
        follow_redirects=False,
    )
    assert "status=app_invalid" in response.headers["location"]
    assert harness.client.app.state.apps.get(remote_id).quality_profile_id == 4


@respx.mock
def test_the_menu_names_every_connection_including_new_ones(harness: AppHarness) -> None:
    """There is no cap on connections and the menu consults all of them — the answer to
    "how many can we add" is "as many as you like, and each one appears"."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    third = harness.client.app.state.apps.add(
        name="Pizza", url="http://pizza.radarr:7878", api_key=KEY)
    pizza_api = "http://pizza.radarr:7878/api/v3"
    respx.get(f"{pizza_api}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "name": "SD"}]))
    respx.get(f"{pizza_api}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/pizza"}]))
    harness.client.app.state.apps.set_defaults(
        third.id, quality_profile_id=2, root_folder="/pizza")
    for api in (LOCAL_API, REMOTE_API, pizza_api):
        respx.get(f"{api}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert ">Main<" in page   # every connection, by name — the primary included
    assert ">4K<" in page
    assert ">Pizza<" in page


@respx.mock
def test_renaming_a_connection_is_reflected_in_the_menu(harness: AppHarness) -> None:
    """The menu reads the live name, so a rename shows up on the next page view with no
    migration and nothing to re-save."""
    harness.activate()
    _seed(harness)
    _local, remote_id = _two_instances(harness)
    for api in (LOCAL_API, REMOTE_API):
        respx.get(f"{api}/movie").mock(return_value=httpx.Response(200, json=[]))

    assert ">4K<" in harness.client.get(f"/reports/{REPORT_ID}").text

    harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "Pizza", "url": REMOTE_URL, "api_key": ""},
        follow_redirects=False,
    )

    page = harness.client.get(f"/reports/{REPORT_ID}").text
    assert ">Pizza<" in page
    assert ">4K<" not in page


@respx.mock
def test_a_renamed_connection_is_named_in_the_library_badge(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness)
    _local, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(
        200, json=[{"tmdbId": TMDB, "id": 9, "hasFile": True, "title": "Gladiator II"}]))
    harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "Pizza", "url": REMOTE_URL, "api_key": ""},
        follow_redirects=False,
    )

    assert "In Library · Pizza" in harness.client.get(f"/reports/{REPORT_ID}").text


def test_an_over_long_name_is_refused_by_the_form(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/apps",
        data={"name": "x" * 200, "url": LOCAL_URL, "api_key": KEY},
        follow_redirects=False,
    )
    assert "status=app_invalid" in response.headers["location"]
    assert harness.client.app.state.apps.list_apps() == []


@respx.mock
def test_one_save_stores_the_name_and_the_defaults_together(harness: AppHarness) -> None:
    """One card, one Save. Two buttons meant editing the name and the quality, pressing
    one, and silently losing the other."""
    harness.activate()
    _local, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    respx.get(f"{REMOTE_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    harness.client.get("/settings")  # warms each connection's options cache

    harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "Pizza", "url": REMOTE_URL, "api_key": "",
              "quality_profile_id": "4", "root_folder": "/mnt/4k"},
        follow_redirects=False,
    )

    saved = harness.client.app.state.apps.get(remote_id)
    assert saved.name == "Pizza"              # identity
    assert saved.quality_profile_id == 4      # and defaults, from the same submit
    assert saved.root_folder == "/mnt/4k"


@respx.mock
def test_a_rejected_default_does_not_half_apply_the_save(harness: AppHarness) -> None:
    # Validation runs before anything is written, so a bad quality cannot leave the name
    # changed and the defaults not.
    harness.activate()
    _local, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    respx.get(f"{REMOTE_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    harness.client.get("/settings")

    harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "Pizza", "url": REMOTE_URL, "api_key": "",
              "quality_profile_id": "77", "root_folder": "/mnt/4k"},
        follow_redirects=False,
    )

    saved = harness.client.app.state.apps.get(remote_id)
    assert saved.name == "4K"                 # nothing was written
    assert saved.quality_profile_id == 4


def test_no_button_is_labelled_save_defaults_any_more(harness: AppHarness) -> None:
    """Three buttons reading "Save Defaults" with different scopes was the confusion.

    Each connection now has one Save for everything about it, and the schedule section
    owns nothing per-Radarr, so the ambiguous label is gone rather than merely rarer.
    """
    harness.activate()
    page = harness.client.get("/settings").text
    assert "Save Defaults" not in page


@respx.mock
def test_a_queued_title_elsewhere_is_wanted_not_in_library(harness: AppHarness) -> None:
    """The badge used to read "In Library · 4K" for a film the 4K box had merely queued,
    because it only checked membership and never `has_file`."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(
        200, json=[{"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text
    assert "Wanted · 4K" in page
    assert "In Library" not in page


# --- the cold-cache fallback must not be a dead end (review step 2) ---


@respx.mock
def test_a_typed_profile_id_saves_when_nothing_is_cached(harness: AppHarness) -> None:
    """A connection added while Radarr was down has no cached profiles, so the card
    renders a plain "Profile ID" number input. What is typed there has to save.

    `all()` over an empty list is True, so the vetting used to reject every value and
    the documented "never a dead-end" fallback was exactly that.
    """
    harness.activate()
    app = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    # No /settings render and Radarr never reached: the options cache stays empty.
    assert harness.client.app.state.radarr_options.load(app.id).is_empty()

    response = harness.client.post(
        f"/settings/apps/{app.id}",
        data={"name": "Main", "url": LOCAL_URL, "api_key": "",
              "quality_profile_id": "4", "root_folder": "/movies"},
        follow_redirects=False,
    )

    assert "status=app_updated" in response.headers["location"]
    saved = harness.client.app.state.apps.get(app.id)
    assert saved.quality_profile_id == 4
    assert saved.root_folder == "/movies"


@respx.mock
def test_a_known_bad_profile_id_is_still_refused(harness: AppHarness) -> None:
    """The relaxation is "cannot check", not "no longer check": once the options ARE
    cached, an id that connection does not offer is still rejected."""
    harness.activate()
    _local, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    respx.get(f"{REMOTE_API}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"}))
    harness.client.get("/settings")  # warms each connection's options cache

    response = harness.client.post(
        f"/settings/apps/{remote_id}",
        data={"name": "4K", "url": REMOTE_URL, "api_key": "",
              "quality_profile_id": "77", "root_folder": "/mnt/4k"},
        follow_redirects=False,
    )

    assert "status=app_invalid" in response.headers["location"]
    assert harness.client.app.state.apps.get(remote_id).quality_profile_id == 4


@respx.mock
def test_a_cold_cache_save_still_rejects_a_non_numeric_profile(harness: AppHarness) -> None:
    """`optional_int` returns None for junk, which stores "no default" rather than
    letting an unparseable value through — the same as leaving the field blank."""
    harness.activate()
    app = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)

    harness.client.post(
        f"/settings/apps/{app.id}",
        data={"name": "Main", "url": LOCAL_URL, "api_key": "",
              "quality_profile_id": "not-a-number", "root_folder": ""},
        follow_redirects=False,
    )

    saved = harness.client.app.state.apps.get(app.id)
    assert saved.quality_profile_id is None
    assert saved.root_folder is None


# --- a stored default survives a Radarr blip (review step 3) ---


@respx.mock
def test_a_stored_default_is_used_when_the_profile_list_is_unavailable(
    harness: AppHarness,
) -> None:
    """The whole point of step 2 + step 3 together: configure while Radarr is down,
    and the add still goes out with the id you configured.

    The id was vetted against THIS connection's Radarr when it was saved, so an empty
    live list means "could not look", not "does not offer it".
    """
    harness.activate()
    _seed(harness)
    app = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=4, root_folder="/movies")
    # Radarr answers the library (so the duplicate check passes) but not the options.
    respx.get(f"{LOCAL_API}/qualityprofile").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/rootfolder").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))

    response = harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II"},
        follow_redirects=False,
    )

    assert "status=added" in response.headers["location"]
    sent = json.loads(add.calls.last.request.content)
    assert sent["qualityProfileId"] == 4
    assert sent["rootFolderPath"] == "/movies"


@respx.mock
def test_a_stale_id_is_still_dropped_once_radarr_answers(harness: AppHarness) -> None:
    """"Cannot check" is not "no longer check": with a real list in hand, an id that
    connection does not offer is still refused and the first offered one is used."""
    harness.activate()
    _seed(harness)
    app = harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=99, root_folder="/movies")  # 99 is not on this box
    respx.get(f"{LOCAL_API}/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{LOCAL_API}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"path": "/movies"}]))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))

    harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II"},
        follow_redirects=False,
    )

    assert json.loads(add.calls.last.request.content)["qualityProfileId"] == 4  # not 99


@respx.mock
def test_a_blip_does_not_grey_out_a_configured_connection(harness: AppHarness) -> None:
    """The menu entry stays clickable, because the advice it used to give was wrong:
    "Pick a quality & folder in Settings" on a connection that already had both."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    # Both boxes answer the library; neither answers the options fetch.
    for api in (LOCAL_API, REMOTE_API):
        respx.get(f"{api}/qualityprofile").mock(return_value=httpx.Response(500))
        respx.get(f"{api}/rootfolder").mock(return_value=httpx.Response(500))
        respx.get(f"{api}/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    # "4K" carries stored defaults (set by _two_instances); "Main" never had any.
    assert _menu_entries(page) == {"Main": False, "4K": True}
    assert "Pick a quality &amp; folder in Settings" in page  # still shown, for Main only


@respx.mock
def test_an_unconfigured_connection_stays_guarded(harness: AppHarness) -> None:
    """Nothing stored and nothing fetched still means nothing to send: the add is
    refused before it reaches Radarr, rather than sending a guessed id."""
    harness.activate()
    _seed(harness)
    harness.client.app.state.apps.add(name="Main", url=LOCAL_URL, api_key=KEY)
    respx.get(f"{LOCAL_API}/qualityprofile").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/rootfolder").mock(return_value=httpx.Response(500))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    add = respx.post(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(201, json={}))

    response = harness.client.post(
        "/add-movie",
        data={"report_id": REPORT_ID, "tmdb_id": str(TMDB), "title": "Gladiator II"},
        follow_redirects=False,
    )

    assert "status=add_config" in response.headers["location"]
    assert not add.called


# --- change quality on whichever connection holds the film (F12) ---


def _held_on_remote(harness: AppHarness, *, has_file: bool = True, profile_id: int = 4) -> None:
    """The film downloaded on the 4K box and absent from the primary — the case the
    change-quality control silently never rendered for."""
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": has_file, "title": "Gladiator II",
         "qualityProfileId": profile_id,
         "movieFile": {"quality": {"quality": {"name": "Bluray-2160p"}}}},
    ]))


def _upgrade_form(page: str) -> str:
    """The change-quality form's markup, or "" when the control did not render."""
    if "/upgrade-movie" not in page:
        return ""
    return page.split('action="/upgrade-movie"')[1].split("</form>")[0]


@respx.mock
def test_a_film_held_only_on_the_secondary_can_still_change_quality(
    harness: AppHarness,
) -> None:
    """The reported gap: `report_detail` reads the PRIMARY's library, so a film only the
    4K box holds had no radarr_id here and the control never rendered at all."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    _held_on_remote(harness)

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert "Already have it on 4K" in page
    assert "(Bluray-2160p)" in page  # what it has there, not what the primary has
    assert 'name="radarr_id" value="9"' in _upgrade_form(page)


@respx.mock
def test_the_select_offers_that_connections_own_profiles(harness: AppHarness) -> None:
    """The whole reason the form carries a connection: profile ids are per database. Both
    boxes here have an id 4 and they are different profiles — offering the primary's list
    would name "HD-1080p" and then send id 4 to a box where 4 is Ultra-HD."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    _held_on_remote(harness)

    form = _upgrade_form(harness.client.get(f"/reports/{REPORT_ID}").text)

    assert "Ultra-HD" in form
    assert "HD-1080p" not in form


@respx.mock
def test_the_upgrade_reaches_the_holding_box_and_not_the_primary(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed(harness)
    local_id, remote_id = _two_instances(harness)
    respx.get(f"{REMOTE_API}/movie/9").mock(return_value=httpx.Response(
        200, json={"id": 9, "qualityProfileId": 4, "monitored": True}))
    remote_put = respx.put(f"{REMOTE_API}/movie/9").mock(
        return_value=httpx.Response(200, json={"id": 9}))
    respx.post(f"{REMOTE_API}/command").mock(return_value=httpx.Response(201, json={}))
    local_put = respx.put(f"{LOCAL_API}/movie/9").mock(
        return_value=httpx.Response(200, json={"id": 9}))

    response = harness.client.post(
        "/upgrade-movie",
        data={"report_id": REPORT_ID, "radarr_id": "9", "quality_profile_id": "4",
              "target": remote_id},
        follow_redirects=False,
    )

    assert "status=upgraded" in response.headers["location"]
    assert remote_put.called
    assert not local_put.called, "a Radarr id means nothing on a box that did not issue it"


@respx.mock
def test_an_unknown_target_is_refused(harness: AppHarness) -> None:
    """The `_resolve_target` rule: the browser picks WHICH configured connection, never
    what it points at."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)

    response = harness.client.post(
        "/upgrade-movie",
        data={"report_id": REPORT_ID, "radarr_id": "9", "quality_profile_id": "4",
              "target": "app-does-not-exist"},
        follow_redirects=False,
    )

    assert "status=add_config" in response.headers["location"]


@respx.mock
def test_a_profile_that_box_does_not_offer_is_refused(harness: AppHarness) -> None:
    """Per-database ids again, on the write side: 99 is on neither box, so it must not be
    sent to one of them."""
    harness.activate()
    _seed(harness)
    _local_id, remote_id = _two_instances(harness)
    remote_put = respx.put(f"{REMOTE_API}/movie/9").mock(
        return_value=httpx.Response(200, json={"id": 9}))

    response = harness.client.post(
        "/upgrade-movie",
        data={"report_id": REPORT_ID, "radarr_id": "9", "quality_profile_id": "99",
              "target": remote_id},
        follow_redirects=False,
    )

    assert "status=add_config" in response.headers["location"]
    assert not remote_put.called


@respx.mock
def test_a_primary_held_film_posts_no_target_and_reads_as_before(
    harness: AppHarness,
) -> None:
    """The acceptance criterion: nothing about the primary's own copy changes. An empty
    target is what this form has always sent, and `_resolve_target` reads it as the
    primary — so the existing path needs no new field to keep working."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 3, "hasFile": True, "title": "Gladiator II",
         "qualityProfileId": 4,
         "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}}},
    ]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))

    form = _upgrade_form(harness.client.get(f"/reports/{REPORT_ID}").text)

    assert 'name="target" value=""' in form
    assert 'name="radarr_id" value="3"' in form
    assert "HD-1080p" in form and "Ultra-HD" not in form
    assert "Already have it (Bluray-1080p) · profile: HD-1080p — change quality" in (
        harness.client.get(f"/reports/{REPORT_ID}").text
    )


@respx.mock
def test_adding_to_the_primary_is_still_offered_alongside(harness: AppHarness) -> None:
    """A film downloaded on the 4K box is still missing from the primary, and that add has
    always been available on this card. The upgrade control appears beside it, not instead
    of it."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    _held_on_remote(harness)

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert "Already have it on 4K" in page
    assert 'action="/add-movie"' in page


@respx.mock
def test_a_queued_copy_elsewhere_offers_no_quality_change(harness: AppHarness) -> None:
    """Changing the quality of a film still on its way means nothing — there is no file to
    replace. The badge already distinguishes the two; the control follows it."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    _held_on_remote(harness, has_file=False)

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert "Wanted · 4K" in page
    assert "/upgrade-movie" not in page


# --- the weekly card says how far along a download is (F13) ---


@respx.mock
def test_a_weekly_card_says_how_far_a_download_has_got(harness: AppHarness) -> None:
    """The weekly view has no location chips, so the fill the dashboard uses has nothing
    to sit on. A card-body line carries it instead — the pattern .card-facts and
    .guess-hint already set, and free to wrap where the badge is not."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([{"movieId": 9, "size": 1_000_000_000, "sizeleft": 580_000_000}])

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert ">42</span>% on 4K" in page
    assert "Wanted · 4K" in page  # the badge is untouched — it shares its row with #1


@respx.mock
def test_the_weekly_badge_never_grows_a_percentage(harness: AppHarness) -> None:
    """Measured: `Wanted · 97% on Living Room 4K` takes the badge from 73px to 198px and
    wraps it to two lines across the rank chip. The percentage goes in the body line."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([{"movieId": 9, "size": 100, "sizeleft": 3}])

    badge = harness.client.get(f"/reports/{REPORT_ID}").text.split('class="badge"')[1]
    badge = badge.split("</div>")[0]

    assert "97%" not in badge
    assert ">97</span>% on 4K" in harness.client.get(f"/reports/{REPORT_ID}").text


@respx.mock
def test_a_weekly_card_with_nothing_downloading_gains_no_line(harness: AppHarness) -> None:
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([])

    assert "dl-line" not in harness.client.get(f"/reports/{REPORT_ID}").text


@respx.mock
def test_a_film_the_primary_is_downloading_still_gets_the_line(harness: AppHarness) -> None:
    """`elsewhere` is emptied when the primary holds the film, which is exactly the case
    this line exists for — so it is read from the holders, not from `elsewhere`."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 3, "hasFile": False, "title": "Gladiator II"}]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    queue_records([{"movieId": 3, "size": 100, "sizeleft": 25}])

    assert ">75</span>% on Main" in harness.client.get(f"/reports/{REPORT_ID}").text


@respx.mock
def test_progress_shows_on_every_week_the_title_charted(harness: AppHarness) -> None:
    """The reported doubt: that progress only appeared on the week the film was added
    from. It never was tied to a week — it is read live from the queue per film — but
    nothing proved it, so here are two weeks holding the same title.
    """
    harness.activate()
    _two_instances(harness)
    for week, report_id in (("2026W30", REPORT_ID), ("2026W31", "report-second-week")):
        harness.client.app.state.reports.save(Report(
            id=report_id, run_at="2026-08-16T12:00:00+00:00", trigger=RunTrigger.MANUAL,
            status=RunStatus.OK, week=week, totals=ReportTotals(movies=1, matched=1),
            movies=[MovieResult(
                rank=1, title="Gladiator II", normalized_title="gladiator 2",
                gross_amount=45_000_000, gross_display="$45.0M", weeks_in_release=1,
                status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=TMDB,
                year=2024)],
        ))
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([{"movieId": 9, "size": 100, "sizeleft": 42}])

    for report_id in (REPORT_ID, "report-second-week"):
        page = harness.client.get(f"/reports/{report_id}").text
        assert ">58</span>% on 4K" in page, f"no progress on {report_id}"


@respx.mock
def test_the_search_results_show_progress_too(harness: AppHarness) -> None:
    """The other surface left out when this shipped. Searching for a title you are waiting
    on is exactly when the question comes up."""
    harness.activate()
    _seed(harness)
    _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([{"movieId": 9, "size": 100, "sizeleft": 25}])

    page = harness.client.get("/reports/search?q=gladiator").text

    assert "Wanted ·" in page
    assert ">75</span>%" in page


@respx.mock
def test_a_downloading_chip_is_keyed_so_it_can_be_kept_current(
    harness: AppHarness,
) -> None:
    """Every indicator names itself the same way, which is what lets one updater drive a
    dashboard chip, a weekly line and the modal without knowing which it has."""
    harness.activate()
    _seed(harness)
    _local_id, remote_id = _two_instances(harness)
    respx.get(f"{LOCAL_API}/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REMOTE_API}/movie").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": TMDB, "id": 9, "hasFile": False, "title": "Gladiator II"}]))
    queue_records([{"movieId": 9, "size": 100, "sizeleft": 50}])

    page = harness.client.get(f"/reports/{REPORT_ID}").text

    assert f'data-progress="{remote_id}:9"' in page
    # The number sits in its own slot, so the poller rewrites a figure and not a sentence.
    assert "<span data-progress-percent>50</span>" in page
