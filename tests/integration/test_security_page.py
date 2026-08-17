"""The Security activity page: what the admin sees about attacks on their box."""

from __future__ import annotations

from app.core.audit import AuditAction
from tests.conftest import AppHarness

SECURITY = "/security"
LOGIN = "/login"


def test_page_lists_failed_and_successful_sign_ins(harness: AppHarness) -> None:
    harness.client.post(
        LOGIN, data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    harness.activate()  # logs in successfully

    page = harness.client.get(SECURITY)
    assert page.status_code == 200
    assert "login_failure" in page.text
    assert "login_success" in page.text
    assert "row-alert" in page.text  # the failure row is flagged
    assert "admin" in page.text


def test_counts_cover_the_last_24h(harness: AppHarness) -> None:
    for _ in range(2):
        harness.client.post(
            LOGIN, data={"username": "admin", "password": "wrong"}, follow_redirects=False
        )
    harness.activate()

    page = harness.client.get(SECURITY)
    assert "Failed (24h)" in page.text
    assert "Logins (24h)" in page.text
    assert "Lockouts (24h)" in page.text


def test_page_requires_authentication(harness: AppHarness) -> None:
    response = harness.client.get(SECURITY, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith(LOGIN)


def test_attacker_supplied_username_is_escaped(harness: AppHarness) -> None:
    # A failed sign-in records whatever username was submitted — it must never render
    # as markup on the page that reports the attack.
    harness.client.post(
        LOGIN,
        data={"username": "<script>alert(1)</script>", "password": "wrong"},
        follow_redirects=False,
    )
    harness.activate()

    page = harness.client.get(SECURITY)
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_empty_state_when_nothing_recorded(harness: AppHarness) -> None:
    harness.activate()
    (harness.settings.logs_dir / "audit.jsonl").unlink()
    page = harness.client.get(SECURITY)
    assert "No recorded activity yet." in page.text


def test_settings_links_to_the_security_page(harness: AppHarness) -> None:
    harness.activate()
    assert "View security activity" in harness.client.get("/settings").text


def test_a_failed_backup_is_highlighted_on_the_security_page(harness: AppHarness) -> None:
    """Recording it is not enough — an unattended failure has to catch the eye, or it
    scrolls past between routine sign-ins."""
    harness.activate()
    harness.client.app.state.audit.record(
        AuditAction.BACKUP_FAILED, reason="scheduled", error="No space left on device"
    )

    page = harness.client.get("/security").text
    assert "backup_failed" in page
    assert "No space left on device" in page
    # The alert class is what the copy promises; a plain row would bury it.
    highlighted = page.split("backup_failed")[0].rsplit("<tr", 1)[-1]
    assert "row-alert" in highlighted


def test_the_page_copy_matches_what_is_actually_highlighted(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/security").text
    assert "Failed sign-ins, lockouts, and failed backups are highlighted." in page
