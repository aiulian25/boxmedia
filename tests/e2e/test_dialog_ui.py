"""The shared detail dialog, run by a real browser, with answers arriving out of order.

The dialog is one element and every title shares it, so what matters is which request gets to
write to it. Each test loads the real app.js into a small page with `fetch` scripted to answer
when told to, under headless Chrome's virtual time.

Skipped where no Chrome is installed, which includes the test image. Needs nothing from the
app, so it runs on its own with `pytest --noconftest tests/e2e/test_dialog_ui.py`.
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

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<a id="a" href="landed.html?title=a" data-movie="1">A</a>
<a id="b" href="landed.html?title=b" data-movie="2">B</a>
<dialog id="movie-dialog" aria-label="Title details"><div data-movie-body></div></dialog>
<pre id="log"></pre>
<script>
  var requests = {{}}, aborted = [];
  var dialog = document.getElementById("movie-dialog");
  window.fetch = function (url, options) {{
    var name = /title=(.)/.exec(url)[1];
    {honour_abort}
    return new Promise(function (resolve, reject) {{
      requests[name] = {{ resolve: resolve, reject: reject }};
      options.signal.addEventListener("abort", function () {{
        aborted.push(name);
        if (window.HONOUR_ABORT) {{ reject(new DOMException("gone", "AbortError")); }}
      }});
    }});
  }};
  function open(name) {{
    var link = document.getElementById(name);
    link.focus();
    link.click();
  }}
  function succeed(name) {{
    requests[name].resolve({{
      ok: true,
      text: function () {{
        return Promise.resolve("<h2>Title " + name + "</h2><p id=body-" + name + "></p>");
      }},
    }});
  }}
  function fail(name) {{ requests[name].reject(new Error("down")); }}
  function refuse(name) {{ requests[name].resolve({{ ok: false, status: 500 }}); }}
  // By hand as well: the real event arrives on a rendered frame, which this run may not
  // produce. If it does arrive, it comes after this and finds nothing left to do.
  function close() {{
    dialog.close();
    dialog.dispatchEvent(new Event("close"));
  }}
  history.back = function () {{}};  // on a file page this can unload the test
  var STEP_MS = 100;
  [{steps}].forEach(function (step, index) {{ setTimeout(step, (index + 1) * STEP_MS); }});
  setTimeout(function () {{
    document.getElementById("log").textContent = JSON.stringify({{
      body: document.querySelector("[data-movie-body]").textContent,
      open: dialog.open,
      label: dialog.getAttribute("aria-label"),
      aborted: aborted,
      focused: document.activeElement && document.activeElement.id,
    }});
  }}, 2000);
</script>
<script src="{script}"></script>
</body></html>
"""

LANDED = """<!doctype html><html><body><pre id="log"></pre><script>
  document.getElementById("log").textContent = JSON.stringify({ navigated: location.search });
</script></body></html>
"""


def _run(tmp_path: Path, *steps: str, honour_abort: bool = False) -> dict:
    (tmp_path / "landed.html").write_text(LANDED, "utf-8")
    page = tmp_path / "page.html"
    page.write_text(PAGE.format(
        honour_abort=f"window.HONOUR_ABORT = {json.dumps(honour_abort)};",
        steps=",".join(f"function () {{ {step} }}" for step in steps),
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


OPEN_A = 'open("a");'
OPEN_B = 'open("b");'
CLOSE = "close();"


def test_a_title_opens_and_the_dialog_is_named_after_it(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, 'succeed("a");')

    assert run["body"] == "Title a"
    assert run["label"] == "Title a"
    assert run["open"] is True


def test_while_it_loads_the_dialog_keeps_its_general_name(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, 'succeed("a");', CLOSE, OPEN_B)

    assert run["body"] == "Loading…"
    assert run["label"] == "Title details"  # not still the title before


def test_a_late_answer_cannot_replace_the_title_now_open(tmp_path: Path) -> None:
    """The audit's reproduction, with a server that answers despite the abort."""
    run = _run(tmp_path, OPEN_A, CLOSE, OPEN_B, 'succeed("b");', 'succeed("a");')

    assert run["body"] == "Title b"
    assert run["label"] == "Title b"


def test_a_late_failure_cannot_navigate_away_from_the_title_now_open(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, CLOSE, OPEN_B, 'succeed("b");', 'fail("a");')

    assert "navigated" not in run
    assert run["body"] == "Title b"
    assert run["open"] is True


def test_a_late_refusal_cannot_navigate_either(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, CLOSE, OPEN_B, 'refuse("a");')

    assert "navigated" not in run
    assert run["body"] == "Loading…"


def test_an_answer_to_a_closed_dialog_writes_nothing(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, CLOSE, 'succeed("a");')

    assert run["body"] == ""
    assert run["open"] is False


def test_a_failure_after_closing_does_not_navigate(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, CLOSE, 'fail("a");')

    assert "navigated" not in run


def test_closing_abandons_the_request(tmp_path: Path) -> None:
    assert _run(tmp_path, OPEN_A, CLOSE)["aborted"] == ["a"]


def test_opening_another_abandons_the_one_before(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_A, OPEN_B, 'succeed("a");', 'succeed("b");')

    assert run["aborted"] == ["a"]
    assert run["body"] == "Title b"


def test_an_abort_that_rejects_is_not_mistaken_for_a_failure(tmp_path: Path) -> None:
    """What a real fetch does when aborted. Read as a failure, closing would navigate."""
    run = _run(tmp_path, OPEN_A, CLOSE, honour_abort=True)

    assert "navigated" not in run


def test_the_current_title_failing_still_falls_back_to_its_page(tmp_path: Path) -> None:
    """The reason the fallback exists: never strand someone in an empty dialog."""
    assert _run(tmp_path, OPEN_A, 'fail("a");')["navigated"] == "?title=a"


def test_the_current_title_being_refused_falls_back_too(tmp_path: Path) -> None:
    assert _run(tmp_path, OPEN_B, 'refuse("b");')["navigated"] == "?title=b"


def test_closing_returns_focus_to_what_opened_it(tmp_path: Path) -> None:
    run = _run(tmp_path, OPEN_B, 'succeed("b");', CLOSE)

    assert run["focused"] == "b"


def test_a_close_event_arriving_after_the_next_title_opened_leaves_it_alone(
    tmp_path: Path,
) -> None:
    """The event is queued, so it can land after the next title is already open."""
    run = _run(
        tmp_path,
        OPEN_A,
        'dialog.close(); open("b"); dialog.dispatchEvent(new Event("close"));',
        'succeed("b");',
    )

    assert run["body"] == "Title b"
    assert run["open"] is True
