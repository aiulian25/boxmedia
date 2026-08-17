"""app.js is untested by design — this pins the contract that makes that safe.

The review left the modal/toast/scroll script without automated coverage rather than
adding a JS test runner for ~120 lines whose failure mode is "falls back to a full page
load". That trade is only sound while the fallback genuinely exists: every control the
script enhances must work with the script blocked. These tests assert that server-side,
without a browser.
"""

from __future__ import annotations

import re

import httpx
import respx

from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from tests.conftest import AppHarness

TMDB_ID = 693134
RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — the suite's dummy key
LOOKUP = {"tmdbId": TMDB_ID, "title": "Dune: Part Two", "year": 2024, "images": [],
          "genres": [], "ratings": {}}
# <a ...> tags carrying the modal hook, so the hook can be checked for a real href.
_MOVIE_HOOK = re.compile(r"<a\b[^>]*\bdata-movie=[^>]*>", re.IGNORECASE)
_TO_TOP = re.compile(r"<button\b[^>]*\bdata-to-top\b[^>]*>", re.IGNORECASE)


def _seed(harness: AppHarness, tmdb_id: int | None) -> None:
    movie = MovieResult(
        rank=1, title="Dune: Part Two", normalized_title="dune part two",
        gross_amount=45_000_000, gross_display="$45.0M", weeks_in_release=1,
        status=MovieStatus.WANTED, action=MovieAction.NONE, tmdb_id=tmdb_id,
    )
    harness.client.app.state.reports.save(Report(
        id="report-20260814-100000-pe01", run_at="2026-08-14T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W27",
        totals=ReportTotals(movies=1, matched=1), movies=[movie],
    ))


def test_every_modal_hook_is_a_real_link(harness: AppHarness) -> None:
    """app.js intercepts `a[data-movie]`. With the script blocked the click must still
    navigate, so the hook may only ever live on an anchor that carries an href."""
    harness.activate()
    _seed(harness, TMDB_ID)

    page = harness.client.get("/reports/report-20260814-100000-pe01").text
    hooks = _MOVIE_HOOK.findall(page)
    assert hooks, "no modal hook rendered — the test would pass vacuously"
    for hook in hooks:
        assert 'href="' in hook, f"data-movie without href: {hook}"


def test_the_link_target_serves_a_full_page_not_only_a_fragment(
    harness: AppHarness,
) -> None:
    # The href app.js hijacks has to be a page in its own right, or JS-off users land on
    # a bare fragment.
    harness.activate()
    page = harness.client.get(f"/movies/{TMDB_ID}").text
    assert "<!DOCTYPE html>" in page
    assert "topnav" in page  # full layout, not the modal fragment


def test_an_unmatched_title_renders_no_dead_hook(harness: AppHarness) -> None:
    # Nothing to open, so there must be neither a hook nor an empty href.
    harness.activate()
    _seed(harness, None)
    page = harness.client.get("/reports/report-20260814-100000-pe01").text
    assert "data-movie=" not in page
    assert 'href=""' not in page


def test_every_action_is_a_form_post_not_a_script_hook(harness: AppHarness) -> None:
    """The toast and scroll-restore enhance POST->303->GET. Every mutating control is a
    real form, so with the script blocked the action still happens — only the polish
    (in-place scroll, auto-dismiss) is lost."""
    harness.activate()
    page = harness.client.get("/settings").text
    assert page.count("<form") >= 5
    assert "onclick=" not in page  # no inline handlers; the CSP forbids them anyway


def test_the_toast_is_server_rendered_so_it_survives_without_js(
    harness: AppHarness,
) -> None:
    # app.js only auto-dismisses it. The message itself must come from the server, or a
    # JS-off admin gets no confirmation at all.
    harness.activate()
    page = harness.client.get("/settings?status=backup_created").text
    assert "data-toast" in page
    assert "Backup created." in page


@respx.mock
def test_the_modals_add_control_works_with_the_script_blocked(harness: AppHarness) -> None:
    """The modal now carries a mutating action, so it falls under the same contract as
    every other one: the dialog is an enhancement, and the film's own page must offer the
    add on its own. A control that only existed inside the JS-injected fragment would be
    the first one in the app a blocked script could take away."""
    harness.activate()
    app = harness.client.app.state.apps.add(
        name="Main", url=RADARR_URL, api_key=RADARR_KEY
    )
    harness.client.app.state.apps.set_defaults(
        app.id, quality_profile_id=4, root_folder="/movies"
    )
    respx.get(f"{RADARR_URL}/api/v3/movie/lookup/tmdb").mock(
        return_value=httpx.Response(200, json=LOOKUP)
    )
    respx.get(f"{RADARR_URL}/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    page = harness.client.get(f"/movies/{TMDB_ID}").text

    assert "<!DOCTYPE html>" in page, "the fallback must be a page, not a fragment"
    assert 'action="/add-movie"' in page
    assert 'method="post"' in page
    assert "onclick=" not in page


def _to_top_tag(page: str) -> str:
    """The scroll-to-top button's opening tag, whole. Sliced by hand it truncates at
    whichever attribute the slice keys on, which is how a check for aria-label passed
    against a string that stopped before it."""
    match = _TO_TOP.search(page)
    assert match, "no scroll-to-top control rendered — the test would pass vacuously"
    return match.group(0)


def test_the_scroll_to_top_control_ships_hidden_on_every_page(harness: AppHarness) -> None:
    """It is revealed by app.js once there is a screenful to come back from. Shipping it
    visible would put a button over the corner of every page including ones too short to
    scroll — and with the script blocked it would never go away again."""
    harness.activate()

    for path in ("/dashboard", "/reports", "/settings"):
        assert "hidden" in _to_top_tag(harness.client.get(path).text), (
            f"scroll-to-top is not hidden on {path}"
        )


def test_the_scroll_to_top_control_is_on_the_signed_out_page_too(
    harness: AppHarness,
) -> None:
    """"All pages" includes the login screen, where it stays inert — it is outside the
    signed-in guard the topnav and the movie dialog sit behind."""
    assert "hidden" in _to_top_tag(harness.client.get("/login").text)


def test_the_scroll_to_top_control_mutates_nothing(harness: AppHarness) -> None:
    """The file's contract is that every control the script enhances still WORKS with the
    script blocked. This one is exempt by being no control at all: it posts nothing,
    changes nothing, and duplicates what Home, a trackpad and a swipe already do — so its
    absence costs a shortcut, not a capability.
    """
    harness.activate()
    tag = _to_top_tag(harness.client.get("/dashboard").text)

    assert 'type="button"' in tag  # never a submit, even inside a form
    assert "formaction" not in tag
    assert 'aria-label="Scroll to top"' in tag  # the glyph beside it is aria-hidden
