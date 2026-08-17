"""Step 24: end-to-end smoke test against the running containerized stack.

Skipped unless BM_E2E_BASE_URL is set (the shell orchestrator provides it after
`docker run` of the real image + the mock Radarr/BOM server). Drives the exact
flow a first-time user would: bootstrap login → forced change → connect Radarr →
test → run → dashboard/report → backup → mutate → restore → verify unchanged.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

BASE_URL = os.environ.get("BM_E2E_BASE_URL")
BOOTSTRAP_PASSWORD = os.environ.get("BM_E2E_BOOTSTRAP_PASSWORD", "")
MOCK_URL = os.environ.get("BM_E2E_MOCK_URL", "")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="BM_E2E_BASE_URL not set")

FIRST_PASSWORD = "e2echanged9pass"
SECOND_PASSWORD = "e2emutated9pass"


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, follow_redirects=False, timeout=30.0)


def _csrf(client: httpx.Client) -> str:
    """The token the rendered page carries — the e2e drives the app like a browser.

    The token is bound to the session, not the page, so any page that actually renders
    will do: before the forced password change only /change-password does, afterwards
    /settings does.
    """
    for path in ("/settings", "/change-password", "/login"):
        page = client.get(path)
        match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
        if match:
            return match.group(1)
    return ""


def _post(client: httpx.Client, path: str, data: dict | None = None, **kwargs) -> httpx.Response:
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf(client))
    return client.post(path, data=payload, **kwargs)


def test_full_stack_smoke() -> None:
    client = _client()

    # 1. Log in with the bootstrap password and complete the forced change.
    login = client.post("/login", data={"username": "admin", "password": BOOTSTRAP_PASSWORD})
    assert login.status_code == 303, login.text
    changed = _post(
        client,
        "/change-password",
        data={"new_password": FIRST_PASSWORD, "confirm_password": FIRST_PASSWORD},
    )
    assert changed.status_code == 303
    # First-run wizard: with no Radarr connection yet, the forced change lands on Settings
    # (app/web/auth.py change_password_submit), not the dashboard.
    assert changed.headers["location"].endswith("/settings"), changed.headers.get("location")
    assert client.get("/dashboard").status_code == 200

    # 2. Connect the (mock) Radarr and test the connection.
    _post(
        client,
        "/settings/apps",
        data={"name": "Radarr - Mock", "url": MOCK_URL, "api_key": "mock-key-123456"},
    )
    settings_html = client.get("/settings").text
    app_id = re.search(r"/settings/apps/(app-[0-9a-f]+)/test", settings_html).group(1)
    tested = _post(client, f"/settings/apps/{app_id}/test")
    assert "test_ok" in tested.headers["location"], tested.headers.get("location")

    # 3. Set the Radarr defaults used when the admin adds a title.
    _post(
        client,
        "/settings/filters",
        data={"quality_profile_id": "4", "default_root_folder": "/movies",
              "schedule_interval_hours": "168"},
    )

    # 4. Run the pipeline now (scrapes the mock BOM chart, reconciles vs mock Radarr).
    run = _post(client, "/run")
    assert run.status_code == 303

    # 5. The run only REPORTS. The library view shows the pre-owned title, and
    #    NOTHING was auto-added — no missing chart title appears as Wanted yet.
    dashboard = client.get("/dashboard").text
    assert "Dune: Part Two" in dashboard
    assert "In Library" in dashboard
    assert "Wanted" not in dashboard

    # 6. A report card exists; open it to add a missing title by hand.
    reports = client.get("/reports").text
    assert "Movies" in reports and "Matched" in reports
    report_id = re.search(r"/reports/(report-[0-9a-z-]+)", reports).group(1)
    detail = client.get(f"/reports/{report_id}").text
    assert "Add to Radarr" in detail  # the per-title add control the review flow needs
    add_form = re.search(
        r'action="[^"]*/add-movie".*?name="tmdb_id" value="(\d+)".*?name="title" value="([^"]+)"',
        detail,
        re.DOTALL,
    )
    added = _post(
        client,
        "/add-movie",
        data={"report_id": report_id, "tmdb_id": add_form.group(1),
              "title": add_form.group(2), "year": "2025"},
    )
    assert "status=added" in added.headers["location"]

    # After the manual add, the title now appears in the library view as Wanted.
    assert "Wanted" in client.get("/dashboard").text

    # 7. Create a backup.
    created = _post(client, "/settings/backups/create")
    assert "backup_created" in created.headers["location"]
    backup_name = re.search(
        r"/settings/backups/(boxmedia-[0-9a-f-]+\.backup)/download", client.get("/settings").text
    ).group(1)

    # 8. Mutate: change the password.
    _post(
        client,
        "/account/password",
        data={"current_password": FIRST_PASSWORD, "new_password": SECOND_PASSWORD,
              "confirm_password": SECOND_PASSWORD},
    )

    # 9. Restore the backup — the pre-mutation password must work again.
    restored = _post(client, f"/settings/backups/{backup_name}/restore")
    assert "backup_restored" in restored.headers["location"]

    fresh = _client()  # new cookie jar
    relogin = fresh.post("/login", data={"username": "admin", "password": FIRST_PASSWORD})
    assert relogin.status_code == 303  # indistinguishable from before the mutation
