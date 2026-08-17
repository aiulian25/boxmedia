"""F19: /favicon.ico is served unauthenticated, like /static and /health."""

from __future__ import annotations

from app.main import FAVICON_MEDIA_TYPE, FAVICON_PATH
from tests.conftest import AppHarness

ICO_MAGIC = b"\x00\x00\x01\x00"


def test_favicon_is_served_without_a_session(harness: AppHarness) -> None:
    harness.client.cookies.clear()
    response = harness.client.get(FAVICON_PATH, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"] == FAVICON_MEDIA_TYPE
    # A real multi-image ICO, not the login page HTML the gate used to hand back.
    assert response.content.startswith(ICO_MAGIC)


def test_favicon_probe_is_not_redirected_to_login(harness: AppHarness) -> None:
    harness.client.cookies.clear()
    assert harness.client.get(FAVICON_PATH, follow_redirects=False).status_code == 200


def test_pages_link_the_icon_set(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/dashboard").text
    assert 'href="/favicon.ico' in page
    assert "/static/logo.png" in page
    assert "favicon.svg" not in page  # the placeholder mark is gone


def test_login_page_shows_the_logo(harness: AppHarness) -> None:
    page = harness.client.get("/login").text
    assert 'class="auth-mark"' in page
    assert "/static/logo.png" in page


def test_favicon_answers_head_probes(harness: AppHarness) -> None:
    # Browsers and caching proxies HEAD the icon; @app.get alone would 405 them.
    harness.client.cookies.clear()
    response = harness.client.head(FAVICON_PATH, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"] == FAVICON_MEDIA_TYPE
