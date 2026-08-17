"""Defaults every page-rendering test wants, and none of them should have to state.

Integration-only on purpose: the unit tests of `RadarrClient.queue` are ABOUT this
endpoint and must see their own answers, not a house default.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

# The dashboard and the weekly view read each connection's download queue to show how far
# a Wanted title has got. A test about anything else must not have to know that, and respx
# refuses an unmocked call — so every host answers "nothing is downloading" by default.
#
# Registered FIRST, and respx resolves the first matching route, so a per-test route added
# later would be shadowed. That is why it is named: a test that cares re-points it through
# `queue_records` rather than adding a second route.
QUEUE_ROUTE = "radarr_queue"
_QUEUE_URL_PATTERN = r"https?://[^/]+/api/v3/queue"


@pytest.fixture(autouse=True)
def _quiet_radarr_queues() -> Iterator[None]:
    respx.get(url__regex=_QUEUE_URL_PATTERN, name=QUEUE_ROUTE).mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    yield
    # Removed by hand, because an integration test that is NOT decorated with
    # `@respx.mock` has no context to tear the route down with — it would be registered on
    # the global router for the rest of the session, and the unit tests of
    # `RadarrClient.queue` would then be answering to this default instead of their own
    # records. That failure only appears in a full run, never when the file runs alone.
    respx.routes.pop(QUEUE_ROUTE, None)


def queue_records(records: list[dict]) -> None:
    """Point every connection's queue at `records` — `{movieId, size, sizeleft}` entries.

    Re-points the default route rather than adding one, because the default is registered
    first and would otherwise win. Same answer for every host, which is what a test with
    one connection wants; a test that needs two boxes to differ registers its own routes
    before touching this.
    """
    respx.routes[QUEUE_ROUTE].mock(
        return_value=httpx.Response(200, json={"records": records})
    )
