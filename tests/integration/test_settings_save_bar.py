"""One Save for the Settings page: which forms it speaks for, and which it must not.

The bar itself is behaviour in app.js, driven in a real browser during development. What
these pin is the CONTRACT the markup and the routes have to keep for that JavaScript to
be correct — and the promise that a browser without it loses nothing.
"""

from __future__ import annotations

import re

from app.web.settings import STATUS_MESSAGES, SettingsStatus
from tests.conftest import AppHarness

# Every form on the page that holds fields you edit and then save. The bar submits these
# and only these; anything else is an ACTION — restore a backup, delete a connection,
# change a password — which is not a value left pending.
SETTINGS_ACTIONS = {
    "/account/profile",
    "/account/theme",
    "/settings/plex",
    "/settings/filters",
    "/settings/region",
    "/settings/backups/schedule",
}
# Actions that must NEVER be swept up by a Save button. The password is the sharpest
# case: it asks for the current one and is a deliberate act, not a pending field.
NEVER_BATCHED = {
    "/account/password",
    "/account/logout-all",
    "/settings/apps",
    "/settings/plex/remove",
    "/settings/plex/refresh",
    "/settings/plex/test",
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
        "/settings/plex",
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


def test_both_outcomes_of_a_batched_save_have_something_to_say(
    harness: AppHarness,
) -> None:
    """The reload lands on one of these two; a status with no message renders a blank
    banner, which is worse than none."""
    assert SettingsStatus.SETTINGS_SAVED in STATUS_MESSAGES
    assert SettingsStatus.SETTINGS_SAVE_FAILED in STATUS_MESSAGES
    assert STATUS_MESSAGES[SettingsStatus.SETTINGS_SAVED][0] == "success"
    assert STATUS_MESSAGES[SettingsStatus.SETTINGS_SAVE_FAILED][0] == "error"


def test_a_rejected_status_is_not_mistaken_for_a_saved_one() -> None:
    """app.js decides "did this card object?" with /saved|_ok$|updated/. Every status a
    settings form can redirect with has to fall on the right side of that."""
    good = re.compile(r"saved|_ok$|updated")
    for status in (SettingsStatus.FILTERS_SAVED, SettingsStatus.REGION_SAVED,
                   SettingsStatus.PLEX_SAVED, SettingsStatus.BACKUP_SCHEDULE_SAVED,
                   SettingsStatus.APP_UPDATED):
        assert good.search(status), f"{status} would be reported as a failure"
    for status in (SettingsStatus.FILTERS_INVALID, SettingsStatus.REGION_INVALID,
                   SettingsStatus.PLEX_INVALID, SettingsStatus.BACKUP_SCHEDULE_INVALID,
                   SettingsStatus.APP_INVALID):
        assert not good.search(status), f"{status} would pass as a success"
