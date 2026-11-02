"""The download poller, run by a real browser.

`/progress`'s contract is only half of this: what an outage looks like on screen is decided
in app.js. Each test loads the real file into a small page with `fetch` scripted, under
headless Chrome's virtual time, so an outage and its recovery take no time to wait out.

Skipped where no Chrome is installed, which includes the test image. Needs nothing from the
app, so it runs on its own with `pytest --noconftest tests/e2e/test_progress_ui.py`.
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
SECOND_MS = 1000
LOG_RE = re.compile(r'<pre id="log">(.*?)</pre>', re.DOTALL)

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome to run the script in")

HEALTHY = {"available": True, "complete": True}
DOWN = {"available": False, "complete": False}
TRUNCATED = {"available": True, "complete": False}
OUTAGE = "reject"  # the request itself fails

PAGE = """<!doctype html>
<html><body data-progress-url="/progress">
{indicators}
<a id="open" href="/movies/1" data-movie="1">film</a>
<dialog id="movie-dialog"><div data-movie-body></div></dialog>
<pre id="log"></pre>
<script>
  var ANSWERS = {answers}, FRAGMENT = {fragment}, polls = [], index = 0;
  function figures() {{
    var seen = {{}};
    document.querySelectorAll("[data-progress]").forEach(function (node) {{
      var slot = node.querySelector("[data-progress-percent]");
      seen[node.getAttribute("data-progress")] = node.hidden ? null : slot.textContent;
    }});
    return seen;
  }}
  window.fetch = function (url) {{
    if (url !== "/progress") {{
      return Promise.resolve({{
        ok: true,
        text: function () {{ return Promise.resolve(FRAGMENT); }},
      }});
    }}
    polls.push({{ at: performance.now(), figures: figures() }});
    var answer = ANSWERS[Math.min(index++, ANSWERS.length - 1)];
    if (answer === "{outage}") {{
      return Promise.reject(new Error("down"));
    }}
    return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve(answer); }} }});
  }};
  {actions}
  setTimeout(function () {{
    document.getElementById("log").textContent = JSON.stringify({{
      polls: polls,
      last: figures(),
    }});
  }}, {finish_at});
</script>
<script src="{script}"></script>
</body></html>
"""


def _indicator(key: str, percent: int) -> str:
    return (
        f'<span class="dl-line" data-progress="{key}">'
        f"<span data-progress-percent>{percent}</span>%</span>"
    )


def _answer(sources: dict, progress: dict | None = None) -> dict:
    return {"sources": sources, "progress": progress or {}}


def _run(
    tmp_path: Path,
    answers: list,
    *,
    indicators: str = "",
    fragment: str = "",
    actions: str = "",
    seconds: int = 120,
) -> dict:
    page = tmp_path / "page.html"
    page.write_text(PAGE.format(
        indicators=indicators,
        answers=json.dumps(answers),
        fragment=json.dumps(fragment),
        actions=actions,
        outage=OUTAGE,
        finish_at=seconds * SECOND_MS,
        script=APP_JS.as_uri(),
    ), "utf-8")
    result = subprocess.run(  # noqa: S603 — our own browser, on a page written just above
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp_path / 'profile'}",
            f"--virtual-time-budget={(seconds + 1) * SECOND_MS}",
            "--dump-dom", page.as_uri(),
        ],
        capture_output=True, text=True, timeout=CHROME_TIMEOUT_SECONDS, check=True,
    )
    return json.loads(html.unescape(LOG_RE.search(result.stdout).group(1)))


def test_an_outage_keeps_the_figure_and_recovery_updates_it_in_the_same_tab(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        [
            _answer({"a": DOWN}),
            OUTAGE,
            _answer({"a": HEALTHY}, {"a:7": 70}),
        ],
        indicators=_indicator("a:7", 40),
    )

    assert run["polls"][1]["figures"] == {"a:7": "40"}  # survived the "down" answer
    assert run["polls"][2]["figures"] == {"a:7": "40"}  # and the failed request
    assert run["last"] == {"a:7": "70"}


def test_a_failed_instance_does_not_erase_another_with_the_same_id(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [_answer({"a": HEALTHY, "b": DOWN})],
        indicators=_indicator("a:7", 40) + _indicator("b:7", 60),
    )

    assert run["last"] == {"a:7": None, "b:7": "60"}


def test_a_truncated_queue_never_says_a_download_finished(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [_answer({"a": TRUNCATED})],
        indicators=_indicator("a:7", 40),
        seconds=60,
    )

    assert run["last"] == {"a:7": "40"}
    assert len(run["polls"]) > 1  # and it is still being asked about


def test_a_whole_answer_clears_a_finished_download_and_polling_stops(tmp_path: Path) -> None:
    run = _run(tmp_path, [_answer({"a": HEALTHY})], indicators=_indicator("a:7", 40))

    assert run["last"] == {"a:7": None}
    assert len(run["polls"]) == 1


def test_a_download_still_running_keeps_polling(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [_answer({"a": HEALTHY}, {"a:7": 50}), _answer({"a": HEALTHY}, {"a:7": 60})],
        indicators=_indicator("a:7", 40),
        seconds=40,
    )

    assert run["last"] == {"a:7": "60"}
    assert len(run["polls"]) == 2


def test_a_dialog_opened_later_starts_polling_and_closing_it_stops(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [_answer({"a": HEALTHY}, {"a:9": 55})],
        fragment=_indicator("a:9", 10),
        actions="""
          setTimeout(function () { document.getElementById("open").click(); }, 1000);
          setTimeout(function () {
            polls.push({ at: performance.now(), figures: figures(), marker: true });
            // Fired by hand: from a timer under virtual time, headless Chrome does not deliver
            // the real one, though a rendered browser does. Back is stubbed because closing
            // spends the entry opening pushed, and on a file page that can unload it.
            history.back = function () {};
            var dialog = document.getElementById("movie-dialog");
            dialog.close();
            dialog.dispatchEvent(new Event("close"));
          }, 40000);
        """,
    )

    marker = next(poll for poll in run["polls"] if poll.get("marker"))
    assert marker["figures"] == {"a:9": "55"}  # updated while it was open
    assert run["polls"].index(marker) == len(run["polls"]) - 1  # and nothing asked after
    assert run["last"] == {}


def test_a_page_with_nothing_to_watch_never_asks(tmp_path: Path) -> None:
    assert _run(tmp_path, [_answer({})], seconds=40)["polls"] == []


def test_a_long_outage_asks_less_often_up_to_a_limit(tmp_path: Path) -> None:
    run = _run(tmp_path, [OUTAGE], indicators=_indicator("a:7", 40), seconds=600)

    times = [poll["at"] for poll in run["polls"]]
    gaps = [
        round((later - earlier) / SECOND_MS)
        for earlier, later in zip(times, times[1:], strict=False)
    ]
    assert gaps[:3] == [30, 60, 120]
    assert set(gaps[3:]) == {120}


def test_a_recovery_goes_back_to_the_normal_pace(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [OUTAGE, OUTAGE, _answer({"a": HEALTHY}, {"a:7": 50})],
        indicators=_indicator("a:7", 40),
        seconds=150,
    )

    times = [poll["at"] for poll in run["polls"]]
    assert round((times[3] - times[2]) / SECOND_MS) == 15
