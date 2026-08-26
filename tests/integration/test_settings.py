"""Step 10 integration test: Settings CRUD, no plaintext key in HTML, Test Connection."""

from __future__ import annotations

import httpx
import respx

from app.web.settings import SettingsStatus
from tests.conftest import AppHarness

RADARR_URL = "http://127.0.0.1:1"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
STATUS_URL = f"{RADARR_URL}/api/v3/system/status"


def _add_app(harness: AppHarness) -> None:
    harness.client.post(
        "/settings/apps",
        data={"name": "Radarr - Main", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )


def test_add_app_then_listed_without_plaintext_key(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/apps",
        data={"name": "Radarr - Main", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Nothing is listening on RADARR_URL in this test, so the add reports exactly that
    # rather than a bare "added" — the connection is still saved either way.
    assert SettingsStatus.APP_ADDED_UNREACHABLE in response.headers["location"]

    page = harness.client.get("/settings")
    assert "Radarr - Main" in page.text
    # The key is never rendered into the page.
    assert RADARR_KEY not in page.text
    # And never stored in plaintext.
    apps_yml = (harness.settings.config_dir / "apps.yml").read_text(encoding="utf-8")
    assert RADARR_KEY not in apps_yml


def test_remove_app(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id
    response = harness.client.post(
        f"/settings/apps/{app_id}/delete", follow_redirects=False
    )
    assert SettingsStatus.APP_REMOVED in response.headers["location"]
    assert harness.client.app.state.apps.list_apps() == []


@respx.mock
def test_connection_success(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json={"version": "5.2"}))
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id
    response = harness.client.post(f"/settings/apps/{app_id}/test", follow_redirects=False)
    assert SettingsStatus.TEST_OK in response.headers["location"]
    assert "app_tested" in "\n".join(harness.audit_lines())


@respx.mock
def test_connection_auth_failure(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id
    response = harness.client.post(f"/settings/apps/{app_id}/test", follow_redirects=False)
    assert SettingsStatus.TEST_AUTH in response.headers["location"]


@respx.mock
def test_connection_unreachable(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(side_effect=httpx.ConnectError("refused"))
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id
    response = harness.client.post(f"/settings/apps/{app_id}/test", follow_redirects=False)
    assert SettingsStatus.TEST_CONN in response.headers["location"]


def test_status_renders_as_autodismissing_toast(harness: AppHarness) -> None:
    # Action confirmations are a one-shot bubble (base.html toast region), not a banner
    # pinned to the top of the page that survives until the admin navigates away.
    harness.activate()
    harness.client.post("/settings/backups/create", follow_redirects=False)
    page = harness.client.get(f"/settings?status={SettingsStatus.BACKUP_CREATED}")
    assert "toast-region" in page.text
    assert "data-toast" in page.text
    assert "Backup created." in page.text
    # The message lives only in the toast — no inline banner above the page content.
    assert page.text.count("Backup created.") == 1


def test_static_script_is_served_and_versioned(harness: AppHarness) -> None:
    # The toast/scroll-restore enhancement is a same-origin file (CSP is script-src 'self').
    page = harness.client.get("/settings")
    assert "/static/js/app.js?v=" in page.text
    served = harness.client.get("/static/js/app.js")
    assert served.status_code == 200
    assert "bm_scroll" in served.text


# --- ignored titles manager (F13) ---


def test_ignored_titles_are_listed_and_reversible(harness: AppHarness) -> None:
    harness.activate()
    ignore = harness.client.app.state.ignore
    ignore.add(tmdb_id=555, title="Neon Rain", normalized_title="neon rain")
    ignore.add(tmdb_id=None, title="Obscure Doc", normalized_title="obscure doc")

    page = harness.client.get("/settings").text
    assert "Ignored Titles" in page
    assert "Neon Rain" in page and "Obscure Doc" in page
    assert "555" in page  # the tmdb id when known

    response = harness.client.post(
        "/unignore",
        data={"tmdb_id": "555", "normalized_title": "neon rain", "next": "settings"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/settings?status=unignored")

    remaining = [movie.title for movie in ignore.list_ignored()]
    assert remaining == ["Obscure Doc"]
    assert "Removed from your ignore list." in harness.client.get(
        "/settings?status=unignored"
    ).text


def test_ignored_title_is_reversible_after_its_report_is_gone(harness: AppHarness) -> None:
    """The dead end this fixes: a title that stopped charting could never be un-ignored,
    because the only control lived on a report card that no longer shows it."""
    harness.activate()
    ignore = harness.client.app.state.ignore
    ignore.add(tmdb_id=None, title="Cookie Queens", normalized_title="cookie queens")
    assert ignore.is_ignored(None, "cookie queens") is True

    harness.client.post(
        "/unignore",
        data={"normalized_title": "cookie queens", "next": "settings"},
        follow_redirects=False,
    )
    # No longer ignored, so the next pipeline run stops flagging it.
    assert ignore.is_ignored(None, "cookie queens") is False
    assert ignore.list_ignored() == []


def test_empty_state_when_nothing_is_ignored(harness: AppHarness) -> None:
    harness.activate()
    assert "Nothing ignored." in harness.client.get("/settings").text


def test_unignore_from_a_report_still_returns_to_that_report(harness: AppHarness) -> None:
    # The existing weekly-view flow is unchanged by the new `next` field.
    harness.activate()
    harness.client.app.state.ignore.add(
        tmdb_id=555, title="Neon Rain", normalized_title="neon rain"
    )
    response = harness.client.post(
        "/unignore",
        data={"report_id": "report-20260814-120000-abcd", "tmdb_id": "555",
              "normalized_title": "neon rain"},
        follow_redirects=False,
    )
    assert "/reports/report-20260814-120000-abcd?status=unignored" in response.headers["location"]


PLAYFUL_HINT = "Have fun with it"
PRACTICAL_NOTE = "How you’ll pick it when sending a title"


def test_the_first_connection_gets_the_playful_naming_nudge(harness: AppHarness) -> None:
    """With nothing configured there is nothing to tell apart, so explaining how to pick
    between instances would be noise. The useful nudge is to pick something memorable now."""
    harness.activate()

    page = harness.client.get("/settings").text

    assert PLAYFUL_HINT in page
    assert PRACTICAL_NOTE not in page


@respx.mock
def test_a_second_connection_gets_the_practical_note(harness: AppHarness) -> None:
    # Once one exists, "how you'll pick it when sending" is finally relevant.
    api = f"{RADARR_URL}/api/v3"
    respx.get(f"{api}/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5"})
    )
    respx.get(f"{api}/qualityprofile").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{api}/rootfolder").mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    harness.client.app.state.apps.add(name="Local", url=RADARR_URL, api_key=RADARR_KEY)

    page = harness.client.get("/settings").text

    assert PRACTICAL_NOTE in page
    assert PLAYFUL_HINT not in page


def test_exactly_one_naming_hint_is_ever_shown(harness: AppHarness) -> None:
    # Never both, never neither — the field always carries guidance, just the right one.
    harness.activate()
    page = harness.client.get("/settings").text
    assert (PLAYFUL_HINT in page) != (PRACTICAL_NOTE in page)


# --- Test Connection before the connection is saved ---

TEST_PATH = "/settings/apps/test"
RADARR_STATUS = {"appName": "Radarr", "version": "6.3.0.10514", "instanceName": "Radarr"}


def _test_credentials(harness: AppHarness, *, url: str = RADARR_URL, key: str = RADARR_KEY):
    return harness.client.post(TEST_PATH, data={"url": url, "api_key": key})


@respx.mock
def test_a_working_connection_names_the_version_it_reached(harness: AppHarness) -> None:
    """"Something answered" is not the same as "Radarr answered". Naming the version is
    what turns the test into proof you have the right box and the right port."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=RADARR_STATUS))

    body = _test_credentials(harness).text

    assert "Radarr 6.3.0.10514 responded" in body
    assert "app-health-ok" in body


@respx.mock
def test_testing_saves_nothing(harness: AppHarness) -> None:
    """The whole point is that it runs BEFORE the decision to store anything."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=RADARR_STATUS))

    _test_credentials(harness)

    assert harness.client.app.state.apps.list_apps() == []
    apps_yml = harness.settings.config_dir / "apps.yml"
    assert not apps_yml.exists() or RADARR_KEY not in apps_yml.read_text(encoding="utf-8")


@respx.mock
def test_the_tested_key_is_never_written_to_the_audit_log(harness: AppHarness) -> None:
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))

    _test_credentials(harness)

    audit = harness.settings.logs_dir / "audit.jsonl"
    assert not audit.exists() or RADARR_KEY not in audit.read_text(encoding="utf-8")


@respx.mock
def test_a_rejected_key_is_told_apart_from_an_unreachable_box(harness: AppHarness) -> None:
    """Two different fixes: one is the key, the other is the address. Saying "could not
    connect" for a 401 sends the user to re-check an address that was right."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))

    body = _test_credentials(harness).text

    assert "rejected the API key" in body
    assert "app-health-auth" in body


@respx.mock
def test_an_unreachable_address_says_so(harness: AppHarness) -> None:
    harness.activate()
    respx.get(STATUS_URL).mock(side_effect=httpx.ConnectError("down"))

    body = _test_credentials(harness).text

    assert "Could not reach it" in body
    assert "app-health-unreachable" in body


@respx.mock
def test_pointing_at_sonarr_is_caught(harness: AppHarness) -> None:
    """Sonarr and Lidarr answer /system/status in the same shape with a 200, so without
    checking the name the reply would claim a Radarr responded when none did."""
    harness.activate()
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )

    body = _test_credentials(harness).text

    assert "not a Radarr" in body
    assert "responded" not in body


@respx.mock
def test_a_version_that_is_not_a_version_is_dropped(harness: AppHarness) -> None:
    """The version is remote-controlled text on its way into a page. It is matched
    against a strict shape rather than escaped and hoped for."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(
        200, json={"appName": "Radarr", "version": "<img src=x onerror=alert(1)>"}
    ))

    body = _test_credentials(harness).text

    assert "Radarr responded" in body     # still a success, just unnamed
    assert "onerror" not in body
    assert "<img" not in body


def test_an_unparseable_address_is_not_a_connection_error(harness: AppHarness) -> None:
    # "http://" has no host to resolve; saving it would be refused the same way.
    harness.activate()

    body = _test_credentials(harness, url="http://").text

    assert "can’t be read" in body


def test_testing_requires_a_session(harness: AppHarness) -> None:
    harness.client.cookies.clear()
    response = harness.client.post(
        TEST_PATH, data={"url": RADARR_URL, "api_key": RADARR_KEY}, follow_redirects=False
    )
    assert response.status_code in (302, 303, 403)


def test_the_test_button_ships_hidden_for_the_no_javascript_path(
    harness: AppHarness,
) -> None:
    """It is revealed by app.js. Rendering it always would give no-JS users a button
    that can only work by echoing their API key back into the page."""
    harness.activate()

    page = harness.client.get("/settings").text
    button = page.split("data-test-connection")[1].split(">")[0]

    assert "hidden" in button
    assert f'formaction="{TEST_PATH}"' in page


@respx.mock
def test_adding_a_working_connection_says_radarr_answered(harness: AppHarness) -> None:
    """The no-JavaScript half of the promise: Add reports what it found, so a broken
    connection can never be added silently."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=RADARR_STATUS))

    response = harness.client.post(
        "/settings/apps",
        data={"name": "Main", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )

    assert SettingsStatus.APP_ADDED_OK in response.headers["location"]
    assert len(harness.client.app.state.apps.list_apps()) == 1


@respx.mock
def test_a_connection_that_does_not_answer_is_still_added(harness: AppHarness) -> None:
    """Not a gate: a Radarr that is switched off, or not built yet, is still worth
    configuring. It just must not look like success."""
    harness.activate()
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))

    response = harness.client.post(
        "/settings/apps",
        data={"name": "Main", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )

    assert SettingsStatus.APP_ADDED_AUTH in response.headers["location"]
    assert len(harness.client.app.state.apps.list_apps()) == 1  # saved anyway


def test_testing_a_connection_is_csrf_guarded(harness: AppHarness) -> None:
    """This route carries an API key, so a cross-site page must not be able to make the
    browser send one. `request` bypasses the harness's automatic token, the way a forged
    form would."""
    harness.activate()

    response = harness.client.request(
        "POST", TEST_PATH, data={"url": RADARR_URL, "api_key": RADARR_KEY}
    )

    assert response.status_code == 403


# --- Appearance sits between User Management and External Apps ---


def test_appearance_sits_between_user_management_and_external_apps(
    harness: AppHarness,
) -> None:
    """The placement is the requirement, so it is the assertion — not a side effect of
    where the block happened to be pasted."""
    harness.activate()

    page = harness.client.get("/settings").text
    # Anchored on the heading markup: the first-run banner also says "External Apps",
    # earlier in the page, and a bare substring search finds that instead.
    def heading(title: str) -> int:
        return page.index(f'class="section-title">{title}<')

    assert heading("User Management") < heading("Appearance") < heading("External Apps")


def test_the_appearance_form_offers_both_themes_and_marks_the_current_one(
    harness: AppHarness,
) -> None:
    harness.activate()

    dark_page = harness.client.get("/settings").text
    section = dark_page.split(">Appearance<")[1].split("</section>")[0]

    assert 'value="dark"' in section and 'value="light"' in section
    assert section.index('value="dark"') < section.index("checked") < section.index('value="light"')

    harness.client.post("/account/theme", data={"theme": "light"}, follow_redirects=False)
    light_section = harness.client.get("/settings").text.split(">Appearance<")[1]
    # The checked marker has moved past the light option's value.
    assert light_section.index('value="light"') < light_section.index("checked")


def test_the_appearance_form_carries_a_csrf_token(harness: AppHarness) -> None:
    harness.activate()

    section = harness.client.get("/settings").text.split(">Appearance<")[1].split("</form>")[0]

    assert 'name="csrf_token"' in section


# --- Sonarr connections (TV step 4) ---

SONARR_URL = "http://127.0.0.1:2"
SONARR_KEY = "fedcba9876543210fedcba9876543210"
SONARR_STATUS_URL = f"{SONARR_URL}/api/v3/system/status"
SONARR_PROFILES_URL = f"{SONARR_URL}/api/v3/qualityprofile"
SONARR_FOLDERS_URL = f"{SONARR_URL}/api/v3/rootfolder"


def _add_sonarr(harness: AppHarness) -> str:
    harness.client.post(
        "/settings/apps",
        data={"name": "Sonarr", "url": SONARR_URL, "api_key": SONARR_KEY, "kind": "sonarr"},
        follow_redirects=False,
    )
    return harness.client.app.state.apps.list_apps("sonarr")[0].id


def test_adding_a_sonarr_persists_its_kind_and_never_echoes_the_key(
    harness: AppHarness,
) -> None:
    harness.activate()
    app_id = _add_sonarr(harness)

    stored = harness.client.app.state.apps.get(app_id)
    assert stored.kind == "sonarr"
    assert stored.name == "Sonarr"

    page = harness.client.get("/settings")
    assert "Sonarr" in page.text
    # The same contract the Radarr card has had since it existed.
    assert SONARR_KEY not in page.text
    apps_yml = (harness.settings.config_dir / "apps.yml").read_text(encoding="utf-8")
    assert SONARR_KEY not in apps_yml


def test_a_sonarr_never_appears_where_a_film_would_be_sent(harness: AppHarness) -> None:
    """The regression contract, checked through the web layer this time: the movie flows
    all call list_apps() bare, and a Sonarr reaching one would offer somewhere to send a
    film that cannot hold one."""
    harness.activate()
    _add_app(harness)
    _add_sonarr(harness)

    apps = harness.client.app.state.apps
    assert [app.name for app in apps.list_apps()] == ["Radarr - Main"]
    assert [app.name for app in apps.list_apps("sonarr")] == ["Sonarr"]
    # But Settings shows both, being where connections are managed.
    page = harness.client.get("/settings")
    assert "Radarr - Main" in page.text and "Sonarr" in page.text


def test_both_cards_are_tagged_with_the_server_they_are_for(harness: AppHarness) -> None:
    """A row of look-alike cards with only the address to tell them apart is how someone
    edits the wrong one."""
    harness.activate()
    _add_app(harness)
    _add_sonarr(harness)

    page = harness.client.get("/settings")
    assert '<span class="tag">Radarr</span>' in page.text
    assert '<span class="tag">Sonarr</span>' in page.text


def test_each_kind_shows_its_own_primary_tag(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    _add_sonarr(harness)

    page = harness.client.get("/settings")
    # Two cards, each effectively primary for its own kind, so the tag appears twice.
    assert page.text.count('<span class="tag">Primary</span>') == 2


def test_the_add_form_offers_both_apps_and_names_both_ports(harness: AppHarness) -> None:
    """Without JavaScript the placeholder still names both ports, so nobody is left
    guessing which one the field wants — the media-server card's contract."""
    harness.activate()
    page = harness.client.get("/settings")

    assert 'value="radarr"' in page.text
    assert 'value="sonarr"' in page.text
    assert "data-app-kind" in page.text
    assert 'data-placeholder-sonarr="192.168.1.100:8989 or https://sonarr.example"' in page.text
    assert "Radarr 192.168.1.100:7878 · Sonarr 192.168.1.100:8989" in page.text


def test_only_a_sonarr_card_carries_the_series_fields(harness: AppHarness) -> None:
    """A film has no seasons and no series type. Absent from a Radarr card entirely
    rather than disabled, so there is nothing to wonder about."""
    harness.activate()
    _add_app(harness)
    radarr_only = harness.client.get("/settings").text
    assert 'name="series_type"' not in radarr_only
    assert 'name="season_folders"' not in radarr_only

    _add_sonarr(harness)
    with_sonarr = harness.client.get("/settings").text
    assert 'name="series_type"' in with_sonarr
    assert 'name="season_folders"' in with_sonarr
    assert 'name="search_on_add"' in with_sonarr
    assert "Adds series as" in with_sonarr
    assert "Adds films as" in with_sonarr


@respx.mock
def test_saving_a_sonarr_card_stores_its_series_options(harness: AppHarness) -> None:
    respx.get(SONARR_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    respx.get(SONARR_PROFILES_URL).mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(SONARR_FOLDERS_URL).mock(
        return_value=httpx.Response(200, json=[{"path": "/tv"}])
    )
    harness.activate()
    app_id = _add_sonarr(harness)
    # Populate the options cache so the vetting has something to vet against.
    harness.client.get("/settings")

    harness.client.post(
        f"/settings/apps/{app_id}",
        data={
            "name": "Sonarr", "url": SONARR_URL, "api_key": "",
            "quality_profile_id": "4", "root_folder": "/tv",
            "series_type": "anime", "season_folders": "on",
        },
        follow_redirects=False,
    )

    stored = harness.client.app.state.apps.get(app_id)
    assert stored.quality_profile_id == 4
    assert stored.root_folder == "/tv"
    assert stored.series_type == "anime"
    assert stored.season_folders is True
    # Absent from the submission because the box was unticked — that is False, not
    # "leave it alone", or a toggle could never be turned off.
    assert stored.search_on_add is False


@respx.mock
def test_sonarr_options_are_fetched_and_cached_in_their_own_file(harness: AppHarness) -> None:
    respx.get(SONARR_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    respx.get(SONARR_PROFILES_URL).mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(SONARR_FOLDERS_URL).mock(
        return_value=httpx.Response(200, json=[{"path": "/tv"}])
    )
    harness.activate()
    app_id = _add_sonarr(harness)

    page = harness.client.get("/settings")
    assert "HD-1080p" in page.text
    assert "/tv" in page.text

    cached = harness.client.app.state.sonarr_options.load(app_id)
    assert [profile.name for profile in cached.profiles] == ["HD-1080p"]
    assert cached.root_folders == ["/tv"]
    # Its own file: a Sonarr profile id means nothing to Radarr.
    assert (harness.settings.config_dir / "sonarr_options.yml").exists()
    assert not harness.client.app.state.radarr_options.load(app_id).profiles


def test_a_series_type_the_server_does_not_offer_is_refused(harness: AppHarness) -> None:
    harness.activate()
    app_id = _add_sonarr(harness)

    response = harness.client.post(
        f"/settings/apps/{app_id}",
        data={
            "name": "Sonarr", "url": SONARR_URL, "api_key": "",
            "quality_profile_id": "", "root_folder": "",
            "series_type": "cartoons",
        },
        follow_redirects=False,
    )
    assert SettingsStatus.APP_INVALID in response.headers["location"]
    assert harness.client.app.state.apps.get(app_id).series_type is None


def test_a_crafted_form_cannot_write_series_options_onto_a_radarr(
    harness: AppHarness,
) -> None:
    """Dropped server-side rather than trusted not to arrive: the card does not render
    the fields, but a hand-made POST can still send them."""
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id

    harness.client.post(
        f"/settings/apps/{app_id}",
        data={
            "name": "Radarr - Main", "url": RADARR_URL, "api_key": "",
            "quality_profile_id": "", "root_folder": "",
            "series_type": "anime", "season_folders": "on", "search_on_add": "on",
        },
        follow_redirects=False,
    )

    stored = harness.client.app.state.apps.get(app_id)
    assert stored.series_type is None
    assert stored.season_folders is None
    assert stored.search_on_add is None


@respx.mock
def test_the_pre_save_test_names_sonarr_when_sonarr_answers(harness: AppHarness) -> None:
    respx.get(SONARR_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    harness.activate()

    response = harness.client.post(
        "/settings/apps/test",
        data={"url": SONARR_URL, "api_key": SONARR_KEY, "kind": "sonarr"},
    )
    assert "Sonarr 4.0.1 responded" in response.text
    # One fragment for both kinds — the sentence must not have hardcoded the other app.
    assert "Radarr" not in response.text


@respx.mock
def test_a_sonarr_card_pointed_at_a_radarr_is_caught(harness: AppHarness) -> None:
    """Both answer /system/status with the same shape, so the name is the only thing
    that distinguishes them. Without this the dot goes green and every later call fails
    somewhere confusing."""
    respx.get(SONARR_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Radarr", "version": "5.2.0"})
    )
    harness.activate()

    response = harness.client.post(
        "/settings/apps/test",
        data={"url": SONARR_URL, "api_key": SONARR_KEY, "kind": "sonarr"},
    )
    assert "it is not a Sonarr" in response.text


@respx.mock
def test_a_radarr_card_pointed_at_a_sonarr_is_still_caught(harness: AppHarness) -> None:
    """The check that already existed, proven not to have been loosened."""
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    harness.activate()

    response = harness.client.post(
        "/settings/apps/test",
        data={"url": RADARR_URL, "api_key": RADARR_KEY},
    )
    assert "it is not a Radarr" in response.text


@respx.mock
def test_a_sonarr_that_rejects_the_key_says_so_as_sonarr(harness: AppHarness) -> None:
    respx.get(SONARR_STATUS_URL).mock(return_value=httpx.Response(401))
    harness.activate()

    response = harness.client.post(
        "/settings/apps/test",
        data={"url": SONARR_URL, "api_key": SONARR_KEY, "kind": "sonarr"},
    )
    assert "Sonarr rejected the API key" in response.text


def test_an_unknown_kind_in_the_test_form_is_refused(harness: AppHarness) -> None:
    """The form only ever submits one of two, so anything else is a bug or an attack —
    and it must not become a probe of an arbitrary address under a made-up label."""
    harness.activate()

    response = harness.client.post(
        "/settings/apps/test",
        data={"url": SONARR_URL, "api_key": SONARR_KEY, "kind": "lidarr"},
    )
    assert "can’t be read" in response.text
    assert "Lidarr" not in response.text


def test_an_unknown_kind_cannot_be_added(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/apps",
        data={"name": "Odd", "url": SONARR_URL, "api_key": SONARR_KEY, "kind": "lidarr"},
        follow_redirects=False,
    )
    assert SettingsStatus.APP_INVALID in response.headers["location"]
    assert harness.client.app.state.apps.list_apps(kind=None) == []


def test_a_form_without_a_kind_still_adds_a_radarr(harness: AppHarness) -> None:
    """The upgrade path for a page cached from before the radio existed."""
    harness.activate()
    _add_app(harness)
    assert harness.client.app.state.apps.list_apps()[0].kind == "radarr"


def test_removing_a_sonarr_forgets_its_cached_options(harness: AppHarness) -> None:
    """Forgetting from the wrong cache would leave a removed connection's profiles on
    disk forever."""
    harness.activate()
    app_id = _add_sonarr(harness)
    from app.services.radarr_options import RadarrOptions

    harness.client.app.state.sonarr_options.save(
        app_id, RadarrOptions.model_validate({"root_folders": ["/tv"]})
    )

    harness.client.post(f"/settings/apps/{app_id}/delete", follow_redirects=False)

    assert harness.client.app.state.sonarr_options.load(app_id).is_empty()


@respx.mock
def test_a_sonarr_health_dot_is_probed_with_a_sonarr_client(harness: AppHarness) -> None:
    """Probing a Sonarr with a Radarr client is refused by the store, so the dot would
    read Unreachable for a server that is answering perfectly well — and the person would
    go looking for a network fault that is not there."""
    respx.get(SONARR_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.1"})
    )
    respx.get(SONARR_PROFILES_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(SONARR_FOLDERS_URL).mock(return_value=httpx.Response(200, json=[]))
    harness.activate()
    _add_sonarr(harness)

    page = harness.client.get("/settings").text

    assert "Connected — Sonarr is responding" in page
    assert "Unreachable — check the address, that Sonarr is running" not in page


def test_a_sonarr_profile_is_vetted_against_the_sonarr_cache(harness: AppHarness) -> None:
    """Vetted against what THIS server reported. Checking a Sonarr's profile id against
    the Radarr cache would find nothing cached, skip the check, and store an id that
    means something else entirely on the box it is sent to."""
    from app.services.radarr_options import RadarrOptions

    harness.activate()
    app_id = _add_sonarr(harness)
    harness.client.app.state.sonarr_options.save(
        app_id,
        RadarrOptions.model_validate(
            {"profiles": [{"id": 4, "name": "HD-1080p"}], "root_folders": ["/tv"]}
        ),
    )

    response = harness.client.post(
        f"/settings/apps/{app_id}",
        data={
            "name": "Sonarr", "url": SONARR_URL, "api_key": "",
            "quality_profile_id": "99", "root_folder": "/tv",
        },
        follow_redirects=False,
    )

    assert SettingsStatus.APP_INVALID in response.headers["location"]
    assert harness.client.app.state.apps.get(app_id).quality_profile_id is None
