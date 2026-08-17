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
