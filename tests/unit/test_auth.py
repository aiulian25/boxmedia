"""Session cookie Path is a single shared expression (Step 8) — set and delete
must agree or a sub-path deployment strands the cookie in the browser."""

from __future__ import annotations

from app.core.config import Settings
from app.web.auth import _cookie_path

SECRET = "x" * 32


def _settings(url_base: str) -> Settings:
    return Settings(
        _env_file=None, session_secret=SECRET, encryption_key_file="/k", url_base=url_base
    )


def test_cookie_path_follows_url_base() -> None:
    assert _cookie_path(_settings("/boxmedia")) == "/boxmedia/"
    assert _cookie_path(_settings("/boxmedia/")) == "/boxmedia/"  # trailing slash normalized
    assert _cookie_path(_settings("")) == "/"
