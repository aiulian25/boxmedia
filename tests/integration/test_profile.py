"""Step 8 test: voluntary password change requires current password; profile update."""

from __future__ import annotations

import pytest

from app.core.sessions import COOKIE_NAME
from app.web.profile import MAX_EMAIL_LENGTH, MAX_USERNAME_LENGTH, ProfileStatus
from tests.conftest import AppHarness, CsrfClient

NEW_PASSWORD = "second9password"


def test_wrong_current_password_rejected_and_audited(harness: AppHarness) -> None:
    active = harness.activate()
    response = harness.client.post(
        "/account/password",
        data={
            "current_password": "not-current",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ProfileStatus.WRONG_CURRENT in response.headers["location"]
    assert "password_change_rejected" in "\n".join(harness.audit_lines())
    # Password unchanged: the active one still verifies.
    assert harness.users.verify_password(active) is True


def test_valid_change_updates_password(harness: AppHarness) -> None:
    active = harness.activate()
    response = harness.client.post(
        "/account/password",
        data={
            "current_password": active,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert ProfileStatus.PASSWORD_CHANGED in response.headers["location"]
    assert harness.users.verify_password(NEW_PASSWORD) is True

    # The new password works on a fresh login.
    harness.client.post("/logout", follow_redirects=False)
    relogin = harness.client.post(
        "/login",
        data={"username": "admin", "password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert relogin.status_code == 303


def test_weak_new_password_rejected(harness: AppHarness) -> None:
    active = harness.activate()
    response = harness.client.post(
        "/account/password",
        data={"current_password": active, "new_password": "weak", "confirm_password": "weak"},
        follow_redirects=False,
    )
    assert ProfileStatus.POLICY in response.headers["location"]


def test_profile_update_persists(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/account/profile",
        data={"username": "admin", "display_name": "Home Admin", "email": "me@home.lan"},
        follow_redirects=False,
    )
    assert ProfileStatus.PROFILE_UPDATED in response.headers["location"]
    user = harness.users.load()
    assert user.display_name == "Home Admin"
    assert user.email == "me@home.lan"


def test_username_can_be_changed_and_used_to_log_in(harness: AppHarness) -> None:
    active = harness.activate()
    response = harness.client.post(
        "/account/profile",
        data={"username": "robin", "display_name": "Robin", "email": "me@home.lan"},
        follow_redirects=False,
    )
    assert ProfileStatus.PROFILE_UPDATED in response.headers["location"]
    assert harness.users.load().username == "robin"

    # The new username works on a fresh login; the old one no longer does.
    harness.client.post("/logout", follow_redirects=False)
    ok = harness.client.post(
        "/login", data={"username": "robin", "password": active}, follow_redirects=False
    )
    assert ok.status_code == 303
    harness.client.post("/logout", follow_redirects=False)
    bad = harness.client.post(
        "/login", data={"username": "admin", "password": active}, follow_redirects=False
    )
    assert bad.status_code == 401


def test_invalid_email_rejected(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/account/profile",
        data={"username": "admin", "display_name": "X", "email": "not-an-email"},
        follow_redirects=False,
    )
    assert ProfileStatus.INVALID_PROFILE in response.headers["location"]


def test_username_with_space_rejected(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/account/profile",
        data={"username": "bad name", "display_name": "X", "email": "me@home.lan"},
        follow_redirects=False,
    )
    assert ProfileStatus.INVALID_PROFILE in response.headers["location"]


OVERSIZED = {
    "username": "u" * (MAX_USERNAME_LENGTH + 1),
    "email": "e" * (MAX_EMAIL_LENGTH + 1) + "@example.test",
}


@pytest.mark.parametrize("field", ["username", "email"])
def test_an_oversized_profile_field_is_rejected(harness: AppHarness, field: str) -> None:
    harness.activate()
    data = {"username": "admin", "display_name": "Admin", "email": "a@b.co"}
    data[field] = OVERSIZED[field]

    response = harness.client.post("/account/profile", data=data, follow_redirects=False)

    assert ProfileStatus.INVALID_PROFILE in response.headers["location"]
    assert harness.client.app.state.users.load().username == "admin"  # unchanged


def test_fields_at_the_limit_are_accepted(harness: AppHarness) -> None:
    harness.activate()
    at_limit_user = "u" * MAX_USERNAME_LENGTH
    response = harness.client.post(
        "/account/profile",
        data={"username": at_limit_user, "display_name": "Admin", "email": "a@b.co"},
        follow_redirects=False,
    )
    assert ProfileStatus.PROFILE_UPDATED in response.headers["location"]
    assert harness.client.app.state.users.load().username == at_limit_user


def test_the_form_surfaces_the_caps(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/settings").text
    assert f'maxlength="{MAX_USERNAME_LENGTH}"' in page
    assert f'maxlength="{MAX_EMAIL_LENGTH}"' in page


# --- Appearance: dark by default, light by choice ---

THEME_PATH = "/account/theme"


def _user_yml(harness: AppHarness) -> str:
    return (harness.settings.config_dir / "user.yml").read_text(encoding="utf-8")


def test_choosing_light_is_stored_and_rendered(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post(THEME_PATH, data={"theme": "light"}, follow_redirects=False)

    assert response.status_code == 303
    assert ProfileStatus.THEME_UPDATED in response.headers["location"]
    assert "theme: light" in _user_yml(harness)
    assert 'class="light"' in harness.client.get("/library").text


def test_the_default_is_dark_and_says_so_in_the_markup(harness: AppHarness) -> None:
    harness.activate()

    assert 'class="dark"' in harness.client.get("/library").text


def test_switching_back_to_dark_works(harness: AppHarness) -> None:
    harness.activate()
    harness.client.post(THEME_PATH, data={"theme": "light"}, follow_redirects=False)

    harness.client.post(THEME_PATH, data={"theme": "dark"}, follow_redirects=False)

    assert 'class="dark"' in harness.client.get("/library").text
    assert "theme: dark" in _user_yml(harness)


def test_an_unknown_theme_is_refused_and_changes_nothing(harness: AppHarness) -> None:
    """The value becomes a class on <html>. It is checked against a closed set before it
    is written, and the template compares rather than interpolates — belt and braces."""
    harness.activate()
    before = _user_yml(harness)

    response = harness.client.post(
        THEME_PATH, data={"theme": "purple"}, follow_redirects=False
    )

    assert ProfileStatus.INVALID_PROFILE in response.headers["location"]
    assert _user_yml(harness) == before
    assert 'class="dark"' in harness.client.get("/library").text


def test_a_crafted_theme_never_reaches_the_markup(harness: AppHarness) -> None:
    harness.activate()

    harness.client.post(
        THEME_PATH, data={"theme": '"><script>alert(1)</script>'}, follow_redirects=False
    )
    page = harness.client.get("/library").text

    assert "<script>alert(1)</script>" not in page
    assert 'class="dark"' in page


def test_a_user_yml_written_before_the_setting_existed_loads_as_dark(
    harness: AppHarness,
) -> None:
    """Every install that predates this. The theme is read with `.get`, unlike the
    required fields — indexing it would fail the load inside the auth gate, on every
    request, and lock the admin out of their own install."""
    harness.activate()
    path = harness.settings.config_dir / "user.yml"
    path.write_text(
        "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                  if not line.startswith("theme:")) + "\n",
        encoding="utf-8",
    )
    assert "theme:" not in path.read_text(encoding="utf-8")

    page = harness.client.get("/library")

    assert page.status_code == 200
    assert 'class="dark"' in page.text


def test_a_theme_this_build_does_not_ship_falls_back_rather_than_failing(
    harness: AppHarness,
) -> None:
    # A hand-edited or newer-build file should render the default look, not 500.
    harness.activate()
    path = harness.settings.config_dir / "user.yml"
    path.write_text(path.read_text(encoding="utf-8").replace("theme: dark", "theme: solarized"),
                    encoding="utf-8")

    page = harness.client.get("/library")

    assert page.status_code == 200
    assert 'class="dark"' in page.text


def test_the_login_page_is_dark_whatever_the_account_stores(harness: AppHarness) -> None:
    """No session, no account context — and the theme is a per-account setting."""
    harness.activate()
    harness.client.post(THEME_PATH, data={"theme": "light"}, follow_redirects=False)
    harness.client.cookies.clear()

    assert 'class="dark"' in harness.client.get("/login").text


def test_the_theme_change_is_audited(harness: AppHarness) -> None:
    harness.activate()

    harness.client.post(THEME_PATH, data={"theme": "light"}, follow_redirects=False)

    assert any(
        '"theme": "light"' in line and "profile_updated" in line
        for line in harness.audit_lines()
    )


def test_changing_the_theme_is_csrf_guarded(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.request("POST", THEME_PATH, data={"theme": "light"})

    assert response.status_code == 403
    assert "theme: dark" in _user_yml(harness)


def test_changing_your_password_ends_every_other_session(harness: AppHarness) -> None:
    """Changing the password is the answer to "somebody may have my cookie", so the copy
    has to stop working now rather than in up to BM_SESSION_TTL_HOURS.

    The caller's OWN id has to change too: a stolen cookie is a copy of that id, so
    sparing it would leave the thief signed in — the one outcome this action exists to
    prevent.
    """
    active = harness.activate()
    stolen = CsrfClient(harness.client.app)
    stolen.post(
        "/login", data={"username": "admin", "password": active}, follow_redirects=False
    )
    assert stolen.get("/library", follow_redirects=False).status_code == 200
    before = harness.client.cookies.get(COOKIE_NAME)

    changed = harness.client.post(
        "/account/password",
        data={
            "current_password": active,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )

    assert changed.status_code == 303
    assert ProfileStatus.PASSWORD_CHANGED in changed.headers["location"]
    # The other holder is out.
    assert (
        stolen.get("/library", follow_redirects=False).headers["location"].endswith("/login")
    )
    # The caller is still in, on a DIFFERENT id.
    after = harness.client.cookies.get(COOKIE_NAME)
    assert after and after != before, "the caller's own session id must be replaced"
    assert harness.client.get("/library", follow_redirects=False).status_code == 200
    assert "sessions_reset" in "\n".join(harness.audit_lines())
    # And it is visible where an admin would look for it, not only in the file.
    assert "sessions_reset=True" in harness.client.get("/security").text


def test_the_caller_can_act_immediately_after_changing_their_password(
    harness: AppHarness,
) -> None:
    """Rotating the id re-derives the CSRF token, so the promise "you stay signed in" has
    to mean the next action actually works, not just that a GET renders."""
    active = harness.activate()
    harness.client.post(
        "/account/password",
        data={
            "current_password": active,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )

    acted = harness.client.post(
        "/account/theme", data={"theme": "light"}, follow_redirects=False
    )

    assert acted.status_code == 303
    assert ProfileStatus.THEME_UPDATED in acted.headers["location"]
