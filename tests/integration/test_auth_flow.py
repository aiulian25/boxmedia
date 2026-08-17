"""Step 7 test: the gate blocks everything until login + forced password change."""

from __future__ import annotations

import pytest

from tests.conftest import AppHarness, CsrfClient

STRONG_PASSWORD = "brandnew9password"
DASHBOARD = "/dashboard"
RADARR_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — the suite's dummy key
LOGIN = "/login"
CHANGE_PW = "/change-password"


def _login(harness: AppHarness, password: str):
    return harness.client.post(
        LOGIN,
        data={"username": "admin", "password": password},
        follow_redirects=False,
    )


def test_health_is_public(harness: AppHarness) -> None:
    response = harness.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unauthenticated_is_redirected_to_login(harness: AppHarness) -> None:
    response = harness.client.get(DASHBOARD, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith(LOGIN)


def test_wrong_password_is_rejected_and_audited(harness: AppHarness) -> None:
    response = _login(harness, "not-the-password")
    assert response.status_code == 401
    assert any("login_failure" in line for line in harness.audit_lines())


def test_forced_change_gate_blocks_until_password_changed(harness: AppHarness) -> None:
    # 1. Login with the bootstrap password succeeds.
    login = _login(harness, harness.bootstrap_password)
    assert login.status_code == 303
    assert login.headers["location"].endswith(DASHBOARD)

    # 2. But every real route now redirects to the forced-change page.
    blocked = harness.client.get(DASHBOARD, follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"].endswith(CHANGE_PW)

    # 3. Mismatched confirmation is rejected.
    mismatch = harness.client.post(
        CHANGE_PW,
        data={"new_password": STRONG_PASSWORD, "confirm_password": "different9x"},
        follow_redirects=False,
    )
    assert mismatch.status_code == 400

    # 4. Weak password is rejected by policy.
    weak = harness.client.post(
        CHANGE_PW,
        data={"new_password": "weak", "confirm_password": "weak"},
        follow_redirects=False,
    )
    assert weak.status_code == 400

    # 5. A valid change unlocks the app. With no Radarr connection configured yet,
    #    the first-run wizard steers to Settings (improvement #2); the dashboard is
    #    reachable regardless, which is what proves the gate has released.
    changed = harness.client.post(
        CHANGE_PW,
        data={"new_password": STRONG_PASSWORD, "confirm_password": STRONG_PASSWORD},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"].endswith("/settings")

    unlocked = harness.client.get(DASHBOARD, follow_redirects=False)
    assert unlocked.status_code == 200
    assert "login_success" in "\n".join(harness.audit_lines())
    assert "password_changed" in "\n".join(harness.audit_lines())


def test_stylesheet_url_is_cache_busted(harness: AppHarness) -> None:
    # Without a version the browser keeps its cached CSS after a rebuild and renders the
    # previous build's layout.
    page = harness.client.get(LOGIN)
    assert "/static/css/app.css?v=" in page.text


def test_logout_invalidates_session(harness: AppHarness) -> None:
    _login(harness, harness.bootstrap_password)
    # Change password so the session is fully usable, then log out.
    harness.client.post(
        CHANGE_PW,
        data={"new_password": STRONG_PASSWORD, "confirm_password": STRONG_PASSWORD},
        follow_redirects=False,
    )
    logout = harness.client.post("/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"].endswith(LOGIN)
    # The deletion cookie carries the same Path the session cookie was set with
    # (root here; the shared _cookie_path keeps them matched under a url_base).
    assert "path=/" in logout.headers.get("set-cookie", "").lower()

    after = harness.client.get(DASHBOARD, follow_redirects=False)
    assert after.headers["location"].endswith(LOGIN)


def test_logout_all_invalidates_other_sessions(harness: AppHarness) -> None:
    # Two concurrent sign-ins (e.g. laptop + phone, or an attacker's copied cookie).
    active = harness.activate()
    second = CsrfClient(harness.client.app)
    second.post("/login", data={"username": "admin", "password": active},
                follow_redirects=False)
    assert second.get(DASHBOARD, follow_redirects=False).status_code == 200

    cleared = harness.client.post("/account/logout-all", follow_redirects=False)
    assert cleared.status_code == 303
    assert cleared.headers["location"].endswith(LOGIN)

    # Both the other client and the caller are signed out.
    assert second.get(DASHBOARD, follow_redirects=False).headers["location"].endswith(LOGIN)
    assert harness.client.get(DASHBOARD, follow_redirects=False).headers["location"].endswith(LOGIN)
    assert "sessions_cleared" in "\n".join(harness.audit_lines())


def test_sign_in_notice_reports_previous_login_and_failures(harness: AppHarness) -> None:
    active = harness.activate()  # first sign-in: nothing to compare against yet
    assert "Last sign-in:" not in harness.client.get(DASHBOARD).text

    harness.client.post("/logout", follow_redirects=False)
    for _ in range(2):
        harness.client.post(
            LOGIN, data={"username": "admin", "password": "wrong"}, follow_redirects=False
        )
    harness.client.post(LOGIN, data={"username": "admin", "password": active},
                        follow_redirects=False)

    page = harness.client.get(DASHBOARD).text
    assert "Last sign-in:" in page
    assert "2 failed sign-in attempt(s) since then" in page
    assert "banner-error" in page  # failures make it the alert style


def test_sign_in_notice_shows_once(harness: AppHarness) -> None:
    active = harness.activate()
    harness.client.post("/logout", follow_redirects=False)
    harness.client.post(LOGIN, data={"username": "admin", "password": active},
                        follow_redirects=False)

    first = harness.client.get(DASHBOARD).text
    assert "Last sign-in:" in first
    assert "banner-success" in first  # no failures -> not an alert
    assert "Last sign-in:" not in harness.client.get(DASHBOARD).text  # one-shot


def test_sign_in_notice_is_not_carried_in_the_url(harness: AppHarness) -> None:
    # The previous IP must not travel through a query string (browser history, access
    # logs), nor be forgeable by crafting a link.
    active = harness.activate()
    harness.client.post("/logout", follow_redirects=False)
    response = harness.client.post(
        LOGIN, data={"username": "admin", "password": active}, follow_redirects=False
    )
    assert "?" not in response.headers["location"]

    forged = harness.client.get(f"{DASHBOARD}?last=1999-01-01&last_ip=evil.example&failed=99")
    assert "evil.example" not in forged.text


def test_wrong_username_is_rejected_and_audited_identically(harness: AppHarness) -> None:
    """An unknown username must be indistinguishable from a known one with a bad password.

    Behavioural half of the timing fix: same status, same rendered body, same audit
    action. (The timing half — Argon2 now runs on both paths — is measured manually;
    asserting wall-clock here would flake on a loaded CI box.)
    """
    wrong_password = harness.client.post(
        "/login",
        data={"username": "admin", "password": "not-the-password"},
        follow_redirects=False,
    )
    wrong_username = harness.client.post(
        "/login",
        data={"username": "nosuchadmin", "password": "not-the-password"},
        follow_redirects=False,
    )

    assert wrong_username.status_code == wrong_password.status_code == 401
    assert wrong_username.text == wrong_password.text  # no oracle in the response either
    failures = [line for line in harness.audit_lines() if "login_failure" in line]
    assert len(failures) == 2
    assert "nosuchadmin" in failures[-1]  # the submitted name is still recorded


def test_unknown_username_still_counts_toward_the_lockout(harness: AppHarness) -> None:
    # The equalizer must not accidentally bypass record_failure for unknown usernames.
    for _ in range(5):
        harness.client.post(
            "/login",
            data={"username": "nosuchadmin", "password": "wrong"},
            follow_redirects=False,
        )
    locked = harness.client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert locked.status_code == 429


def test_static_requests_never_touch_the_account_file(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate exempts /static, /health and /favicon.ico before any account lookup."""
    harness.activate()
    store = harness.client.app.state.users
    calls = [0]
    original = store.load

    def counting() -> object:
        calls[0] += 1
        return original()

    monkeypatch.setattr(store, "load", counting)

    for path in ("/static/js/app.js", "/health", "/favicon.ico"):
        harness.client.get(path)
    assert calls[0] == 0

    harness.client.get("/dashboard")
    assert calls[0] == 1  # a real page still resolves the account, exactly once


def test_health_answers_head_as_well_as_get(harness: AppHarness) -> None:
    """Uptime monitors commonly probe with HEAD; a 405 there reads as 'service down'."""
    get = harness.client.get("/health")
    head = harness.client.head("/health")

    assert get.status_code == head.status_code == 200
    assert head.content == b""  # HEAD carries no body, by definition
    # Same headers a monitor would match on, minus the body.
    assert head.headers["content-type"] == get.headers["content-type"]


def test_health_head_needs_no_session(harness: AppHarness) -> None:
    # The gate exempts /health by path, not by method — pin that so the exemption check
    # is never narrowed to GET only.
    harness.client.cookies.clear()
    assert harness.client.head("/health", follow_redirects=False).status_code == 200


def test_an_unauthenticated_login_cannot_write_a_huge_audit_entry(
    harness: AppHarness,
) -> None:
    """The half the profile cap does not reach: /login takes a username with no session.

    Bounding only the authenticated profile form would leave this open, and rotation
    means the damage is not a full disk but the loss of the real security history.
    """
    harness.client.post(
        "/login",
        data={"username": "x" * 100_000, "password": "wrong"},
        follow_redirects=False,
    )

    lines = harness.audit_lines()
    assert any("login_failure" in line for line in lines)
    assert max(len(line) for line in lines) < 1024


# --- the sign-in notice uses the app's one transient-message treatment ---


def _toast_markup(page: str) -> str:
    """The toast region, or "" when the page renders none."""
    if 'class="toast-region"' not in page:
        return ""
    return page.split('class="toast-region"')[1].split("</div>")[0]


def test_the_sign_in_notice_is_a_toast_not_a_page_banner(harness: AppHarness) -> None:
    """It used to render inline in the content flow: full width, and sitting there until
    the user navigated away. Every other transient message in the app is a corner toast
    that fades itself out, and this is now the same one."""
    active = harness.activate()
    harness.client.post("/logout", follow_redirects=False)
    harness.client.post(LOGIN, data={"username": "admin", "password": active},
                        follow_redirects=False)

    page = harness.client.get(DASHBOARD).text

    toast = _toast_markup(page)
    assert "Last sign-in:" in toast          # inside the region, not the content flow
    assert "data-toast" in toast             # so app.js removes it on the shared timer
    assert "toast" in toast.split("Last sign-in:")[0].rsplit("<p", 1)[1]  # the size/position class


def test_the_notice_is_the_only_thing_outside_the_toast_region(harness: AppHarness) -> None:
    # Nothing left behind in the page body — the old inline banner is gone, not duplicated.
    active = harness.activate()
    harness.client.post("/logout", follow_redirects=False)
    harness.client.post(LOGIN, data={"username": "admin", "password": active},
                        follow_redirects=False)

    page = harness.client.get(DASHBOARD).text

    assert page.count("Last sign-in:") == 1


def test_the_failed_attempt_link_survives_the_move(harness: AppHarness) -> None:
    """The toast keeps the shortcut to the durable record — which is what makes a
    self-dismissing security notice acceptable rather than lossy."""
    active = harness.activate()
    harness.client.post("/logout", follow_redirects=False)
    harness.client.post(LOGIN, data={"username": "admin", "password": "wrong"},
                        follow_redirects=False)
    harness.client.post(LOGIN, data={"username": "admin", "password": active},
                        follow_redirects=False)

    toast = _toast_markup(harness.client.get(DASHBOARD).text)

    assert "banner-error" in toast
    assert '/security">security activity</a>' in toast


def test_an_unreachable_radarr_stays_a_page_banner(harness: AppHarness) -> None:
    """Not everything becomes a toast. An unreachable connection is a persistent STATE,
    not a one-shot confirmation, so it must not fade away after three seconds."""
    harness.activate()
    harness.client.app.state.apps.add(
        name="Main", url="http://127.0.0.1:1", api_key=RADARR_KEY
    )

    page = harness.client.get(DASHBOARD).text

    assert "Couldn’t reach Main" in page
    assert "Couldn’t reach Main" not in _toast_markup(page)
