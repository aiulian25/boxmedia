"""Shared fixtures: an isolated BoxMedia app + TestClient per test.

Flat files make `tmp_path` the throwaway-database equivalent — each test gets a
fresh data dir, its own encryption key, and a pre-bootstrapped admin whose
one-time password the test knows (so it can log in).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import crypto, filestore, security
from app.core.audit import AuditLog
from app.core.config import Settings
from app.core.sessions import COOKIE_NAME
from app.main import create_app
from app.services.users import UserStore

SESSION_SECRET = "test-secret-" + "x" * 32
ACTIVE_PASSWORD = "activated9password"


class CsrfClient(TestClient):
    """A TestClient that submits the CSRF token a real form would carry.

    Every rendered form includes it (base.html and friends), so posting without one is
    not a realistic browser flow. Tests that want to prove the guard rejects a missing or
    forged token call `client.request("POST", ...)` instead, which bypasses this.
    """

    def post(self, url: str, *, data: dict | None = None, **kwargs: object):  # type: ignore[override]
        token = security.csrf_token_for(SESSION_SECRET, self.cookies.get(COOKIE_NAME, ""))
        payload = dict(data or {})
        payload.setdefault("csrf_token", token)
        return super().request("POST", url, data=payload, **kwargs)


@pytest.fixture(autouse=True)
def _release_data_dir_claims() -> Iterator[None]:
    """A real process holds its claim for life; this one builds a new app per test, and each
    would otherwise keep a descriptor open until the suite ran out of them."""
    yield
    for descriptor in filestore._owned_data_dirs.values():
        os.close(descriptor)
    filestore._owned_data_dirs.clear()


@dataclass
class AppHarness:
    client: TestClient
    settings: Settings
    users: UserStore
    bootstrap_password: str

    def audit_lines(self) -> list[str]:
        path = self.settings.logs_dir / "audit.jsonl"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def activate(self) -> str:
        """Log in and complete the forced password change. Returns the live password."""
        self.client.post(
            "/login",
            data={"username": "admin", "password": self.bootstrap_password},
            follow_redirects=False,
        )
        self.client.post(
            "/change-password",
            data={"new_password": ACTIVE_PASSWORD, "confirm_password": ACTIVE_PASSWORD},
            follow_redirects=False,
        )
        return ACTIVE_PASSWORD


def fetch_queued_posters(harness: AppHarness) -> None:
    """Finish the poster downloads the last page handed off, as the server would a moment
    after responding. TestClient tears its event loop down with each request, so work
    started there does not reliably run to completion; the queue keeps every URL until it
    has been tried, so draining it here is exactly what the background would have done."""
    asyncio.run(harness.client.app.state.poster_prefetcher._drain())


def build_harness(tmp_path: Path, **overrides: object) -> AppHarness:
    """An isolated app + client. `overrides` are extra Settings fields, so a test can
    exercise a non-default policy (e.g. build_harness(tmp_path, login_max_attempts=2))."""
    key_file = tmp_path / "boxmedia.key"
    assert crypto._main(["genkey", str(key_file)]) == 0

    settings = Settings(
        _env_file=None,
        session_secret=SESSION_SECRET,
        encryption_key_file=key_file,
        data_dir=tmp_path / "data",
        secure_cookies=False,  # TestClient talks plain http
        **overrides,
    )
    settings.ensure_data_dirs()

    # Pre-bootstrap so the test knows the one-time password; create_app then finds
    # the account already present and does not re-bootstrap.
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    users = UserStore(settings.config_dir, audit=audit)
    bootstrap_password = users.bootstrap_if_missing()
    assert bootstrap_password is not None

    app = create_app(settings)
    return AppHarness(
        client=CsrfClient(app),
        settings=settings,
        users=users,
        bootstrap_password=bootstrap_password,
    )


@pytest.fixture
def harness(tmp_path: Path) -> AppHarness:
    return build_harness(tmp_path)
