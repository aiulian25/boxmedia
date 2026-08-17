"""Improvement #8: live per-connection health indicator on the Settings page."""

from __future__ import annotations

import asyncio
import time

import httpx
import respx

from app.web import settings as settings_routes
from app.web.deps import RadarrBackoff
from app.web.settings import SettingsStatus
from tests.conftest import AppHarness

RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"
STATUS_URL = f"{RADARR_URL}/api/v3/system/status"
PROFILES_URL = f"{RADARR_URL}/api/v3/qualityprofile"
FOLDERS_URL = f"{RADARR_URL}/api/v3/rootfolder"


def _add_app(harness: AppHarness) -> None:
    harness.client.app.state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)


def _mock_options(response: httpx.Response | None = None) -> None:
    # The Settings page also fetches profiles + folders for the dropdowns.
    respx.get(PROFILES_URL).mock(return_value=response or httpx.Response(200, json=[]))
    respx.get(FOLDERS_URL).mock(return_value=response or httpx.Response(200, json=[]))


@respx.mock
def test_health_shows_connected(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json={"version": "5.2"}))
    _mock_options()
    harness.activate()
    _add_app(harness)
    page = harness.client.get("/settings").text
    assert "Connected" in page


@respx.mock
def test_health_shows_key_rejected(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))
    _mock_options(httpx.Response(401))
    harness.activate()
    _add_app(harness)
    page = harness.client.get("/settings").text
    assert "API key rejected" in page


@respx.mock
def test_health_shows_unreachable(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(side_effect=httpx.ConnectError("refused"))
    respx.get(PROFILES_URL).mock(side_effect=httpx.ConnectError("refused"))
    respx.get(FOLDERS_URL).mock(side_effect=httpx.ConnectError("refused"))
    harness.activate()
    _add_app(harness)
    page = harness.client.get("/settings").text
    assert "Unreachable" in page


def test_no_apps_no_health_probe(harness: AppHarness) -> None:
    harness.activate()
    # No connections configured: the page renders without any probe.
    page = harness.client.get("/settings")
    assert page.status_code == 200
    assert "Welcome to BoxMedia" in page.text


# --- Test Connection is time-bounded like the probes (review step 8) ---

TEST_PATH_TEMPLATE = "/settings/apps/{app_id}/test"
# Comfortably longer than HEALTH_TIMEOUT_SECONDS, so an unbounded call would outlast the
# assertion window while a bounded one gives up well inside it.
HANG_SECONDS = 30.0
# The real bound is HEALTH_TIMEOUT_SECONDS (4s); shortened under test so proving it
# exists does not cost the suite four seconds.
SHORT_BOUND_SECONDS = 0.3


@respx.mock
async def test_test_connection_gives_up_at_the_health_timeout(
    harness: AppHarness, monkeypatch
) -> None:  # noqa: ANN001
    """A hostname stuck in resolution used to hold the request for the OS resolver's
    timeout: httpx's own timeout does not cover name resolution, which is exactly why
    the on-load probes are wrapped in asyncio.wait_for. The button was not.

    The bound is shortened here rather than waited out, which also pins that it comes
    from HEALTH_TIMEOUT_SECONDS — a hard-coded number would ignore the patch and the
    request would run for the full HANG_SECONDS.
    """
    monkeypatch.setattr(settings_routes, "HEALTH_TIMEOUT_SECONDS", SHORT_BOUND_SECONDS)

    async def never_answers(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        await asyncio.sleep(HANG_SECONDS)
        return httpx.Response(200, json={"version": "5.2"})

    respx.get(STATUS_URL).mock(side_effect=never_answers)
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id

    started = time.perf_counter()
    response = harness.client.post(
        TEST_PATH_TEMPLATE.format(app_id=app_id), follow_redirects=False
    )
    elapsed = time.perf_counter() - started

    assert SettingsStatus.TEST_CONN in response.headers["location"]
    assert elapsed < HANG_SECONDS / 2  # bounded, not waiting the host out
    assert "unreachable" in "\n".join(harness.audit_lines())


@respx.mock
def test_test_connection_still_reports_a_rejected_key(harness: AppHarness) -> None:
    # The new TimeoutError clause must not swallow the auth case, which is checked first.
    respx.get(STATUS_URL).mock(return_value=httpx.Response(401))
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id

    response = harness.client.post(
        TEST_PATH_TEMPLATE.format(app_id=app_id), follow_redirects=False
    )

    assert SettingsStatus.TEST_AUTH in response.headers["location"]


@respx.mock
def test_test_connection_still_reports_success(harness: AppHarness) -> None:
    respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json={"version": "5.2"}))
    harness.activate()
    _add_app(harness)
    app_id = harness.client.app.state.apps.list_apps()[0].id

    response = harness.client.post(
        TEST_PATH_TEMPLATE.format(app_id=app_id), follow_redirects=False
    )

    assert SettingsStatus.TEST_OK in response.headers["location"]


# --- a down connection stops being asked on every page view ---

LIBRARY_URL = f"{RADARR_URL}/api/v3/movie"


@respx.mock
def test_a_dead_connection_is_asked_once_not_once_per_page(harness: AppHarness) -> None:
    """The reported cost: a down Radarr charged the full 4s timeout to every dashboard,
    weekly view, search modal and movie modal, for as long as it stayed down."""
    harness.activate()
    _add_app(harness)
    library = respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))

    harness.client.get("/dashboard")
    assert library.call_count == 1

    harness.client.get("/dashboard")
    harness.client.get("/dashboard")

    assert library.call_count == 1, "the dead connection was asked again"


@respx.mock
def test_the_page_still_says_the_connection_is_unreachable(harness: AppHarness) -> None:
    """The skip returns the same None the failure does, so the banner that names the
    down box keeps appearing — a quiet page would be worse than a slow one."""
    harness.activate()
    _add_app(harness)
    respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))

    harness.client.get("/dashboard")
    page = harness.client.get("/dashboard").text

    assert "Couldn’t reach" in page or "Couldn't reach" in page
    assert "Radarr" in page


@respx.mock
def test_a_recovering_connection_is_tried_again_after_the_wait(harness: AppHarness) -> None:
    """The backoff expires. Driven by shortening the window rather than sleeping, so the
    test states the rule instead of waiting for it."""
    harness.activate()
    _add_app(harness)
    harness.client.app.state.radarr_backoff = RadarrBackoff(
        retry_after_seconds=0.0
    )
    library = respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))

    harness.client.get("/dashboard")
    harness.client.get("/dashboard")

    assert library.call_count == 2


@respx.mock
def test_the_health_dots_always_really_try(harness: AppHarness) -> None:
    """The exemption that matters: a backoff which silenced the probes would hide
    recovery instead of surviving an outage. Settings must answer "is it back?"."""
    harness.activate()
    _add_app(harness)
    respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))
    status = respx.get(STATUS_URL).mock(side_effect=httpx.ConnectError("down"))
    _mock_options(httpx.Response(500))

    harness.client.get("/dashboard")  # arms the backoff
    before = status.call_count
    harness.client.get("/settings")
    harness.client.get("/settings")

    assert status.call_count >= before + 2, "the health probe was skipped"


@respx.mock
def test_test_connection_always_really_tries(harness: AppHarness) -> None:
    harness.activate()
    _add_app(harness)
    respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))
    status = respx.get(STATUS_URL).mock(side_effect=httpx.ConnectError("down"))
    app_id = harness.client.app.state.apps.list_apps()[0].id

    harness.client.get("/dashboard")  # arms the backoff
    before = status.call_count
    harness.client.post(f"/settings/apps/{app_id}/test", follow_redirects=False)

    assert status.call_count == before + 1


@respx.mock
def test_editing_a_connection_clears_its_backoff(harness: AppHarness) -> None:
    """Correcting the address or the key is the admin saying "try again" — waiting out
    the window after a fix would look like the fix did not work."""
    harness.activate()
    _add_app(harness)
    library = respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))
    app_id = harness.client.app.state.apps.list_apps()[0].id

    harness.client.get("/dashboard")
    assert library.call_count == 1

    harness.client.post(
        f"/settings/apps/{app_id}",
        data={"name": "Radarr", "url": RADARR_URL, "api_key": RADARR_KEY},
        follow_redirects=False,
    )
    harness.client.get("/dashboard")

    assert library.call_count == 2


@respx.mock
def test_a_healthy_connection_is_never_skipped(harness: AppHarness) -> None:
    # The backoff must not accumulate against a box that keeps answering.
    harness.activate()
    _add_app(harness)
    library = respx.get(LIBRARY_URL).mock(return_value=httpx.Response(200, json=[]))

    for _ in range(3):
        harness.client.get("/dashboard")

    assert library.call_count == 3


@respx.mock
def test_one_dead_connection_does_not_silence_a_healthy_one(harness: AppHarness) -> None:
    """Keyed per connection, not per app: the whole point of the multi-Radarr work is
    that one box being down leaves the other usable."""
    harness.activate()
    _add_app(harness)
    second = "http://second.local:7878"
    harness.client.app.state.apps.add(name="Second", url=second, api_key=RADARR_KEY)
    dead = respx.get(LIBRARY_URL).mock(side_effect=httpx.ConnectError("down"))
    alive = respx.get(f"{second}/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    harness.client.get("/dashboard")
    harness.client.get("/dashboard")

    assert dead.call_count == 1
    assert alive.call_count == 2
