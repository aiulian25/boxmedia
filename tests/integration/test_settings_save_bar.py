"""One Save for the Settings page: which forms it speaks for, and which it must not.

The bar itself is behaviour in app.js, driven in a real browser by
tests/e2e/test_settings_ui.py. What these pin is the CONTRACT the markup and the routes have
to keep for that JavaScript to be correct — and the promise that a browser without it loses
nothing.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app.web.profile import ProfileStatus
from app.web.settings import STATUS_MESSAGES, SettingsStatus
from tests.conftest import AppHarness

# Every form on the page that holds fields you edit and then save. The bar submits these
# and only these; anything else is an ACTION — restore a backup, delete a connection,
# change a password — which is not a value left pending.
SETTINGS_ACTIONS = {
    "/account/profile",
    "/account/theme",
    "/settings/media-server",
    # Two credentials you type and then save, like every other card here. Its Test
    # buttons live in the same form so they can read what was typed, but they are
    # submit buttons — and the bar posts `new FormData(form)`, which excludes those,
    # so pressing Save sends the two key fields and nothing else.
    "/settings/discovery",
    "/settings/filters",
    "/settings/region",
    "/settings/upcoming",
    "/settings/backups/schedule",
}
# Actions that must NEVER be swept up by a Save button. The password is the sharpest
# case: it asks for the current one and is a deliberate act, not a pending field.
NEVER_BATCHED = {
    "/account/password",
    "/account/logout-all",
    "/settings/apps",
    "/settings/media-server/remove",
    "/settings/media-server/refresh",
    "/settings/media-server/test",
    "/settings/discovery/test",
    "/settings/discovery/tmdb/delete",
    "/settings/discovery/trakt/delete",
    "/settings/backups/create",
    "/settings/backups/import",
    "/settings/maintenance/prune-posters",
    "/settings/maintenance/clear-snapshots",
    "/unignore",
}

_FORM_RE = re.compile(r'<form[^>]*action="([^"]*)"[^>]*>')


def _forms(page: str) -> dict[str, str]:
    """Each form's action mapped to its whole opening tag."""
    return {
        match.group(1): match.group(0) for match in _FORM_RE.finditer(page)
    }


def _fully_configured(harness: AppHarness) -> dict[str, str]:
    """The page with everything on it: several forms only exist once connected."""
    from tests.integration.test_reports import FIX_RADARR_URL, RADARR_KEY

    harness.activate()
    harness.client.app.state.apps.add(name="Main", url=FIX_RADARR_URL, api_key=RADARR_KEY)
    harness.client.post(
        "/settings/discovery",
        data={"tmdb_key": "a" * 32, "trakt_client_id": "b" * 42},
    )
    harness.client.post(
        "/settings/media-server",
        data={"url": "http://plex.local:32400", "token": "t" * 20},
        follow_redirects=False,
    )
    return _forms(harness.client.get("/settings").text)


def test_every_settings_form_is_marked_and_no_action_form_is(harness: AppHarness) -> None:
    """Stated over every form the page renders, not a list of known ones: a form added
    later is caught by this whether or not anybody remembered to name it here."""
    forms = _fully_configured(harness)
    per_app = {action for action in forms if action.startswith("/settings/apps/app-")}
    editable = SETTINGS_ACTIONS | {a for a in per_app if a.count("/") == 3}

    for action, tag in forms.items():
        marked = "data-settings-form" in tag
        if action in editable:
            assert marked, f"{action} holds fields the Save bar would never write"
        else:
            assert not marked, (
                f"{action} is an action, not a setting — a Save button must not hold it"
            )
    # And the ones that must be there really were, so this cannot pass on an empty page.
    assert SETTINGS_ACTIONS <= set(forms)
    assert {"/account/password", "/settings/backups/create"} <= set(forms)


def test_a_radarr_card_is_saved_by_the_bar_too(harness: AppHarness) -> None:
    """Only rendered once a connection exists, so it is checked separately."""
    forms = _fully_configured(harness)
    card = next(action for action in forms if action.count("/") == 3
                and action.startswith("/settings/apps/"))

    assert "data-settings-form" in forms[card]
    # Its siblings act on the connection rather than edit it.
    for suffix in ("test", "delete"):
        assert "data-settings-form" not in forms[f"{card}/{suffix}"]


def test_every_saved_form_carries_a_button_the_bar_can_hide(harness: AppHarness) -> None:
    """The page keeps working without JavaScript: each card still has its own Save, and
    app.js hides them only once it is there to speak for all of them."""
    harness.activate()
    page = harness.client.get("/settings").text

    # The Radarr card is absent until a connection exists, so this counts the rest.
    assert page.count("data-form-save") == len(SETTINGS_ACTIONS)


def test_the_bar_ships_hidden_and_names_both_choices(harness: AppHarness) -> None:
    """Hidden markup is the no-JavaScript contract; a bar that only offered Save would
    leave "I did not mean that" with nowhere to go."""
    harness.activate()

    page = harness.client.get("/settings").text

    bar = page[page.index("<div class=\"save-bar\""):]
    bar = bar[: bar.index("</div>")]
    assert "hidden" in bar
    assert "data-save-all" in bar
    assert "data-discard-all" in bar


def test_the_bar_carries_the_url_rather_than_letting_the_script_build_one(
    harness: AppHarness,
) -> None:
    """app.js has no idea what url_base is — every address it uses comes off an element,
    or a sub-path deployment would reload itself to a 404."""
    harness.activate()

    page = harness.client.get("/settings").text

    assert 'data-settings-url="/settings"' in page


def test_a_batched_save_has_something_to_say(harness: AppHarness) -> None:
    """The reload lands here; a status with no message renders a blank banner."""
    assert STATUS_MESSAGES[SettingsStatus.SETTINGS_SAVED][0] == "success"


def _statuses_on(page: str) -> dict[str, list[str]]:
    attribute = re.search(r"data-save-statuses='([^']*)'", page).group(1)
    return json.loads(html.unescape(attribute))


def test_the_bar_carries_the_servers_own_table_of_outcomes(harness: AppHarness) -> None:
    """app.js decides "did this card object?" from this, so a status added to the table cannot
    be misread by a second list in the script."""
    harness.activate()

    statuses = _statuses_on(harness.client.get("/settings").text)

    assert statuses[ProfileStatus.PROFILE_UPDATED][0] == "success"
    assert statuses[SettingsStatus.FILTERS_INVALID][0] == "error"
    assert statuses == {code: list(entry) for code, entry in STATUS_MESSAGES.items()}


def test_a_name_with_quotes_cannot_break_out_of_the_bars_markup(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.apps.add(
        name="""it's "<b>loud""", url="http://radarr.local:7878", api_key="a" * 32
    )

    page = harness.client.get("/settings").text

    assert "<b>loud" not in page
    assert _statuses_on(page)  # and the table still parses


def _location_status(response: object) -> str:
    return parse_qs(urlparse(response.headers["location"]).query)["status"][0]


@pytest.mark.parametrize(
    ("action", "data", "kind"),
    [
        ("/account/profile",
         {"username": "admin", "display_name": "Admin", "email": "a@example.com"}, "success"),
        ("/account/profile", {"username": "a b", "display_name": "A", "email": "x"}, "error"),
        ("/account/theme", {"theme": "nonsense"}, "error"),
        ("/settings/region", {"boxoffice_region": "GB"}, "success"),
        ("/settings/region", {"boxoffice_region": "XX"}, "error"),
        ("/settings/backups/schedule", {"backup_interval_days": "x", "backup_keep": "x"}, "error"),
        ("/settings/media-server", {"url": "not a url", "token": ""}, "error"),
    ],
    ids=["profile", "bad-profile", "bad-theme", "region", "bad-region", "bad-schedule",
         "bad-server"],
)
def test_each_answer_a_card_gives_is_in_the_table_as_what_it_means(
    harness: AppHarness, action: str, data: dict, kind: str
) -> None:
    """Stated against the real routes. A status missing from the table reads as "not
    confirmed", so this is what keeps a good save from being reported as a failed one."""
    harness.activate()

    response = harness.client.post(action, data=data, follow_redirects=False)

    assert response.status_code == 303
    assert STATUS_MESSAGES[_location_status(response)][0] == kind


def test_every_status_the_cards_can_answer_with_is_known() -> None:
    for status in (SettingsStatus.FILTERS_SAVED, SettingsStatus.REGION_SAVED,
                   SettingsStatus.SERVER_SAVED, SettingsStatus.BACKUP_SCHEDULE_SAVED,
                   SettingsStatus.APP_UPDATED, SettingsStatus.DISCOVERY_SAVED,
                   ProfileStatus.PROFILE_UPDATED, ProfileStatus.THEME_UPDATED):
        assert STATUS_MESSAGES[status][0] == "success", f"{status} would be reported as failed"
    for status in (SettingsStatus.FILTERS_INVALID, SettingsStatus.REGION_INVALID,
                   SettingsStatus.SERVER_INVALID, SettingsStatus.BACKUP_SCHEDULE_INVALID,
                   SettingsStatus.APP_INVALID, SettingsStatus.DISCOVERY_INVALID,
                   ProfileStatus.INVALID_PROFILE):
        assert STATUS_MESSAGES[status][0] == "error", f"{status} would pass as a success"


def test_a_save_with_no_session_lands_on_login_which_the_bar_will_not_read_as_saved(
    harness: AppHarness,
) -> None:
    harness.activate()
    harness.client.cookies.clear()

    response = harness.client.post(
        "/settings/region", data={"boxoffice_region": "GB"}, follow_redirects=False
    )

    assert "/login" in response.headers["location"]
    assert "status=" not in response.headers["location"]


def test_a_required_field_left_out_is_a_422_and_not_a_redirect(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post("/account/profile", data={}, follow_redirects=False)

    assert response.status_code == 422


def test_each_card_names_itself_for_the_bars_messages(harness: AppHarness) -> None:
    forms = _fully_configured(harness)

    for action in SETTINGS_ACTIONS:
        assert re.search(r'data-settings-form="[^"]+"', forms[action]), action
