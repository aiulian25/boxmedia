"""The nav shows the display name, not the login name, on every page."""

from __future__ import annotations

from app.web.profile import MAX_DISPLAY_NAME_LENGTH, ProfileStatus
from tests.conftest import AppHarness

EVERY_PAGE = ("/dashboard", "/reports", "/settings", "/security")


def _set_display_name(harness: AppHarness, display_name: str):  # noqa: ANN202
    return harness.client.post(
        "/account/profile",
        data={"username": "admin", "display_name": display_name, "email": "a@b.co"},
        follow_redirects=False,
    )


def test_chip_shows_the_display_name_on_every_page(harness: AppHarness) -> None:
    harness.activate()
    _set_display_name(harness, "Robin Vale")

    for path in EVERY_PAGE:
        page = harness.client.get(path).text
        assert 'class="user-chip"' in page, path
        assert "Robin Vale" in page, path
        assert ">RV<" in page, path  # the initials disc


def test_chip_prefers_the_display_name_over_the_login_name(harness: AppHarness) -> None:
    harness.activate()
    _set_display_name(harness, "Robin Vale")

    chip = harness.client.get("/dashboard").text.split('class="user-chip"')[1].split("</div>")[0]
    assert "Robin Vale" in chip
    assert "admin" not in chip  # the login name is not what the bar shows


def test_login_page_has_no_chip(harness: AppHarness) -> None:
    # No session, no user — the chip must not render an empty disc.
    assert 'class="user-chip"' not in harness.client.get("/login").text


def test_display_name_is_escaped_not_executed(harness: AppHarness) -> None:
    harness.activate()
    _set_display_name(harness, "<script>alert(1)</script>")

    page = harness.client.get("/dashboard").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_over_long_display_name_is_rejected(harness: AppHarness) -> None:
    harness.activate()
    _set_display_name(harness, "Robin Vale")

    response = _set_display_name(harness, "x" * (MAX_DISPLAY_NAME_LENGTH + 1))
    assert ProfileStatus.INVALID_PROFILE in response.headers["location"]
    assert "Robin Vale" in harness.client.get("/dashboard").text  # unchanged


def test_a_display_name_at_the_limit_is_accepted(harness: AppHarness) -> None:
    harness.activate()
    at_limit = "y" * MAX_DISPLAY_NAME_LENGTH
    response = _set_display_name(harness, at_limit)
    assert ProfileStatus.PROFILE_UPDATED in response.headers["location"]
    assert at_limit in harness.client.get("/dashboard").text
