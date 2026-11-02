"""The Settings Save bar, run by a real browser.

Whether a rejected save reads as a success is decided in app.js, so these execute it: the real
file in a small page with `fetch` scripted, under headless Chrome. What the routes really
answer is pinned in tests/integration/test_settings_save_bar.py.

Skipped where no Chrome is installed, which includes the test image. Needs nothing from the
app, so it runs on its own with `pytest --noconftest tests/e2e/test_settings_ui.py`.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "app.js"
CHROME = next(filter(None, map(shutil.which, ("google-chrome", "chromium"))), None)
CHROME_TIMEOUT_SECONDS = 60
LOG_RE = re.compile(r'<pre id="log">(.*?)</pre>', re.DOTALL)

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome to run the script in")

SAVED = {"status": 200, "landed": "settings", "code": "region_saved"}
REJECTED = {"status": 200, "landed": "settings", "code": "region_invalid"}
UNKNOWN_CODE = {"status": 200, "landed": "settings", "code": "something_else"}
NO_CODE = {"status": 200, "landed": "settings", "code": None}
LOGIN_PAGE = {"status": 200, "landed": "login", "code": None}
UNREACHABLE = "unreachable"
REJECTION_TEXT = "That is not a region this build supports."

STATUSES = {
    "region_saved": ["success", "Saved."],
    "region_invalid": ["error", REJECTION_TEXT],
    "settings_saved": ["success", "Settings saved."],
}

PAGE = """<!doctype html>
<html><body>
<form action="/first" data-settings-form="First card">
  <input type="hidden" name="csrf_token" value="old">
  <input id="first" name="first" value="one" required>
  <button data-form-save>Save</button>
</form>
<form action="/second" data-settings-form="Second card">
  <input id="second" name="second" value="two">
  <button data-form-save>Save</button>
</form>
<form action="/third" data-settings-form="Third card">
  <input id="third" name="third" value="three">
  <button data-form-save>Save</button>
</form>
<div data-save-bar data-settings-url="{landed}" data-save-statuses='{statuses}' hidden>
  <span data-save-text>You have unsaved changes.</span>
  <button type="button" data-discard-all>Discard</button>
  <button type="button" data-save-all>Save Changes</button>
</div>
<pre id="log"></pre>
<script>
  var ANSWERS = {answers}, posted = [], tokens = [], gets = 0, index = 0;
  var FRESH_PAGE = {fresh_page};
  var SETTINGS = new URL("{landed}", location.href).href, LOGIN = SETTINGS + ".login";
  window.fetch = function (url, options) {{
    if (!options.method) {{
      gets += 1;
      return Promise.resolve({{ text: function () {{ return Promise.resolve(FRESH_PAGE); }} }});
    }}
    posted.push(url);
    var sent = document.querySelector("form[action='" + url + "'] [name=csrf_token]");
    tokens.push(sent && sent.value);
    window.name = JSON.stringify(posted);
    var answer = ANSWERS[Math.min(index++, ANSWERS.length - 1)];
    if (answer === "{unreachable}") {{
      return Promise.reject(new TypeError("network"));
    }}
    var landed = answer.landed === "login" ? LOGIN : SETTINGS;
    return Promise.resolve({{
      ok: answer.status < 400,
      status: answer.status,
      url: answer.code ? landed + "?status=" + answer.code : landed,
    }});
  }};
  function edit(id, value) {{
    var field = document.getElementById(id);
    field.value = value;
    field.dispatchEvent(new Event("input", {{ bubbles: true }}));
  }}
  function save() {{ document.querySelector("[data-save-all]").click(); }}
  function discard() {{ document.querySelector("[data-discard-all]").click(); }}
  setTimeout(function () {{ {actions} }}, 50);
  setTimeout(function () {{
    var errors = document.querySelectorAll("[data-save-error]");
    document.getElementById("log").textContent = JSON.stringify({{
      posted: posted,
      tokens: tokens,
      gets: gets,
      bar: document.querySelector("[data-save-text]").textContent,
      barHidden: document.querySelector("[data-save-bar]").hidden,
      saveEnabled: !document.querySelector("[data-save-all]").disabled,
      errors: Array.prototype.map.call(errors, function (node) {{ return node.textContent; }}),
      first: document.getElementById("first").value,
      second: document.getElementById("second").value,
    }});
  }}, 2000);
</script>
<script src="{script}"></script>
</body></html>
"""

# Where a successful save reloads to, so a reload can be told from staying put.
LANDED = """<!doctype html><html><body><pre id="log"></pre><script>
  document.getElementById("log").textContent = JSON.stringify({
    reloaded: location.search, posted: JSON.parse(window.name || "[]"),
  });
</script></body></html>
"""


def _run(
    tmp_path: Path,
    answers: list,
    actions: str,
    fresh_page: str = '<input name="csrf_token" value="new">',
) -> dict:
    (tmp_path / "settings.html").write_text(LANDED, "utf-8")
    page = tmp_path / "page.html"
    page.write_text(PAGE.format(
        landed="settings.html",
        statuses=json.dumps(STATUSES),
        answers=json.dumps(answers),
        fresh_page=json.dumps(fresh_page),
        unreachable=UNREACHABLE,
        actions=actions,
        script=APP_JS.as_uri(),
    ), "utf-8")
    result = subprocess.run(  # noqa: S603 — our own browser, on a page written just above
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--virtual-time-budget=3000", "--dump-dom", page.as_uri(),
        ],
        capture_output=True, text=True, timeout=CHROME_TIMEOUT_SECONDS, check=True,
    )
    return json.loads(html.unescape(LOG_RE.search(result.stdout).group(1)))


EDIT_FIRST_AND_SAVE = 'edit("first", "edited"); save();'
EDIT_BOTH_AND_SAVE = 'edit("first", "edited"); edit("second", "also"); save();'


def test_a_valid_save_posts_only_what_changed_and_reloads_saying_so(tmp_path: Path) -> None:
    run = _run(tmp_path, [SAVED], EDIT_BOTH_AND_SAVE)

    assert run["posted"] == ["/first", "/second"]
    assert run["reloaded"] == "?status=settings_saved"


def test_a_blank_required_field_posts_nothing_and_keeps_the_edit(tmp_path: Path) -> None:
    """The audit's reproduction: this came back with the old value and a green banner."""
    run = _run(tmp_path, [SAVED], 'edit("second", "also"); edit("first", ""); save();')

    assert run["posted"] == []  # not even the valid card
    assert run["first"] == ""
    assert run["second"] == "also"
    assert "Nothing was saved" in run["bar"]
    assert "First card" in run["bar"]
    assert run["saveEnabled"] is True


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 422, "landed": "settings", "code": None},
        {"status": 500, "landed": "settings", "code": None},
        {"status": 500, "landed": "settings", "code": "region_saved"},
        LOGIN_PAGE,
        UNREACHABLE,
        UNKNOWN_CODE,
        NO_CODE,
    ],
    ids=[
        "422", "500", "500-claiming-success", "expired-session", "network",
        "unknown-status", "no-status",
    ],
)
def test_a_save_not_positively_confirmed_stays_on_the_page_with_the_edit(
    tmp_path: Path, answer: object
) -> None:
    run = _run(tmp_path, [answer], EDIT_BOTH_AND_SAVE)

    assert "reloaded" not in run
    assert run["posted"] == ["/first"]  # the second card was not sent after that
    assert run["first"] == "edited"
    assert run["second"] == "also"
    assert "Not saved: First card" in run["bar"]
    assert "Saved:" not in run["bar"]
    assert len(run["errors"]) == 1
    assert run["errors"][0].startswith("First card: ")
    assert run["saveEnabled"] is True


def test_a_session_that_ended_says_so(tmp_path: Path) -> None:
    run = _run(tmp_path, [LOGIN_PAGE], EDIT_FIRST_AND_SAVE)

    assert "session has ended" in run["errors"][0]


def test_a_rejected_value_shows_the_servers_own_reason(tmp_path: Path) -> None:
    run = _run(tmp_path, [REJECTED], EDIT_FIRST_AND_SAVE)

    assert run["errors"] == [f"First card: {REJECTION_TEXT}"]


def test_a_second_card_failing_says_which_saved_and_which_did_not(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [SAVED, REJECTED],
        'edit("first", "edited"); edit("second", "also"); edit("third", "more"); save();',
    )

    assert run["posted"] == ["/first", "/second"]  # stopped there: third was not sent
    assert run["bar"] == "Saved: First card. Not saved: Second card."
    assert run["errors"] == [f"Second card: {REJECTION_TEXT}"]
    assert run["second"] == "also"


def test_trying_again_sends_only_what_is_still_unsaved(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [SAVED, REJECTED, SAVED],
        EDIT_BOTH_AND_SAVE + " setTimeout(save, 200);",
    )

    assert run["posted"] == ["/first", "/second", "/second"]
    assert run["reloaded"] == "?status=settings_saved"


def test_correcting_a_card_clears_its_error(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [REJECTED],
        EDIT_FIRST_AND_SAVE + ' setTimeout(function () { edit("first", "fixed"); }, 200);',
    )

    assert run["errors"] == []


def test_discard_after_a_partial_save_reloads_what_is_stored(tmp_path: Path) -> None:
    """reset() would put a saved card's old value back on screen, over what is stored."""
    run = _run(
        tmp_path,
        [SAVED, REJECTED],
        EDIT_BOTH_AND_SAVE + " setTimeout(discard, 200);",
    )

    assert run["reloaded"] == ""


def test_discard_with_nothing_saved_stays_and_restores_the_fields(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [REJECTED],
        EDIT_FIRST_AND_SAVE + " setTimeout(discard, 200);",
    )

    assert "reloaded" not in run
    assert run["first"] == "one"
    assert run["errors"] == []
    assert run["barHidden"] is True
    assert run["bar"] == "You have unsaved changes."


REFUSED = {"status": 403, "landed": "settings", "code": None}


def test_a_stale_token_is_renewed_and_the_save_tried_again_once(tmp_path: Path) -> None:
    """What signing back in elsewhere leaves behind: the form's token was for the old session."""
    run = _run(tmp_path, [REFUSED, SAVED], EDIT_FIRST_AND_SAVE)

    assert run["reloaded"] == "?status=settings_saved"
    assert run["posted"] == ["/first", "/first"]


def test_a_second_refusal_is_reported_and_keeps_the_edit(tmp_path: Path) -> None:
    run = _run(tmp_path, [REFUSED, REFUSED], EDIT_FIRST_AND_SAVE)

    assert run["posted"] == ["/first", "/first"]
    assert run["gets"] == 1
    assert run["first"] == "edited"
    assert "Not saved: First card" in run["bar"]


def test_no_new_token_to_be_had_is_reported_and_keeps_the_edit(tmp_path: Path) -> None:
    """Still signed out: the page fetched is the login form's, or nothing useful."""
    run = _run(tmp_path, [REFUSED], EDIT_FIRST_AND_SAVE, fresh_page="<p>sign in</p>")

    assert run["posted"] == ["/first"]
    assert run["first"] == "edited"
    assert "Not saved: First card" in run["bar"]


def test_a_renewed_token_goes_out_with_the_retry(tmp_path: Path) -> None:
    run = _run(tmp_path, [REFUSED, UNREACHABLE], EDIT_FIRST_AND_SAVE)

    assert run["tokens"] == ["old", "new"]

