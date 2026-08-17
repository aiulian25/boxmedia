"""Improvement #2: first-run mini-wizard steers a new admin to Settings."""

from __future__ import annotations

from tests.conftest import AppHarness

NEW_PASSWORD = "wizardsetup9pass"
RADARR_URL = "http://127.0.0.1:1"
RADARR_KEY = "0123456789abcdef0123456789abcdef"


def test_forced_change_redirects_to_settings_when_no_connection(harness: AppHarness) -> None:
    harness.client.post(
        "/login",
        data={"username": "admin", "password": harness.bootstrap_password},
        follow_redirects=False,
    )
    changed = harness.client.post(
        "/change-password",
        data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"].endswith("/settings")


def test_settings_shows_welcome_banner_until_connection_added(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/settings")
    assert "Welcome to BoxMedia" in page.text

    # Once a connection exists, the welcome banner is gone.
    harness.client.post(
        "/settings/apps",
        data={"name": "Radarr", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )
    after = harness.client.get("/settings")
    assert "Welcome to BoxMedia" not in after.text


def test_forced_change_goes_to_dashboard_when_connection_exists(harness: AppHarness) -> None:
    # Pre-seed a connection, then run the forced-change flow.
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)
    harness.client.post(
        "/login",
        data={"username": "admin", "password": harness.bootstrap_password},
        follow_redirects=False,
    )
    changed = harness.client.post(
        "/change-password",
        data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert changed.headers["location"].endswith("/dashboard")
