"""Step 21 integration test: headers on all routes, lockout, cross-origin rejection."""

from __future__ import annotations

import re

from tests.conftest import AppHarness, build_harness


def test_security_headers_present(harness: AppHarness) -> None:
    response = harness.client.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_headers_on_redirects_too(harness: AppHarness) -> None:
    # An unauthenticated protected route 303s — headers must still be applied.
    response = harness.client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert "content-security-policy" in response.headers


def test_login_lockout_after_five_failures(harness: AppHarness) -> None:
    for _ in range(5):
        harness.client.post(
            "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
        )
    locked = harness.client.post(
        "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert locked.status_code == 429
    assert "login_locked" in "\n".join(harness.audit_lines())
    # Even the correct password is refused while locked.
    still_locked = harness.client.post(
        "/login",
        data={"username": "admin", "password": harness.bootstrap_password},
        follow_redirects=False,
    )
    assert still_locked.status_code == 429


def test_cross_origin_post_rejected(harness: AppHarness) -> None:
    response = harness.client.post(
        "/login",
        data={"username": "admin", "password": "x"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_same_origin_post_allowed(harness: AppHarness) -> None:
    # Origin matching the Host header passes the same-origin check.
    response = harness.client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 401  # reached the handler (bad password), not 403


def test_opaque_null_origin_allowed(harness: AppHarness) -> None:
    # Browsers send Origin: null in sandboxed/privacy contexts; it must not be blocked
    # (SameSite=Strict is the real CSRF defense). Reaches the handler, not a 403.
    response = harness.client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        headers={"origin": "null"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_forwarded_host_origin_allowed(harness: AppHarness) -> None:
    # Behind a reverse proxy the browser Origin is the public host; the container's
    # Host is the backend. Origin matching X-Forwarded-Host must be accepted.
    response = harness.client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        headers={
            "origin": "https://boxmedia.example.com",
            "x-forwarded-host": "boxmedia.example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_mutating_post_without_csrf_token_is_rejected(harness: AppHarness) -> None:
    # `.request` bypasses the harness client's automatic token injection, simulating a
    # forged cross-site POST that rides the session cookie.
    harness.activate()
    response = harness.client.request(
        "POST", "/settings/backups/create", data={}, follow_redirects=False
    )
    assert response.status_code == 403
    assert harness.client.app.state.backups.list_backups() == []  # nothing happened


def test_mutating_post_with_csrf_token_succeeds(harness: AppHarness) -> None:
    harness.activate()
    # Scrape the token out of the rendered page exactly as a browser would submit it.
    page = harness.client.get("/settings")
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text).group(1)
    response = harness.client.request(
        "POST", "/settings/backups/create", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert len(harness.client.app.state.backups.list_backups()) == 1


def test_null_origin_no_longer_bypasses_csrf(harness: AppHarness) -> None:
    # The Origin: null allowance (sandboxed browsers) is no longer a free pass: without
    # the per-session token the request is refused.
    harness.activate()
    response = harness.client.request(
        "POST",
        "/reports/report-does-not-exist/delete",
        data={},
        headers={"Origin": "null"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_forged_csrf_token_is_rejected(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.request(
        "POST", "/settings/backups/create", data={"csrf_token": "0" * 64},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_login_is_exempt_from_csrf(harness: AppHarness) -> None:
    # No session exists yet, so there is no token to bind to; login keeps the Origin
    # check and the rate limiter as its defenses.
    response = harness.client.request(
        "POST",
        "/login",
        data={"username": "admin", "password": harness.bootstrap_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_lockout_threshold_follows_configuration(tmp_path) -> None:
    # BM_LOGIN_MAX_ATTEMPTS=2 must lock on the second failure, not the built-in fifth.
    harness = build_harness(tmp_path, login_max_attempts=2)
    first = harness.client.post(
        "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert first.status_code == 401  # rejected, not yet locked
    second = harness.client.post(
        "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert second.status_code == 401
    locked = harness.client.post(
        "/login",
        data={"username": "admin", "password": harness.bootstrap_password},
        follow_redirects=False,
    )
    assert locked.status_code == 429  # even the CORRECT password is refused while locked
    assert "Too many failed attempts" in locked.text
    assert "login_locked" in "\n".join(harness.audit_lines())
