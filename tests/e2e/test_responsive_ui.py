"""The top bar at the widths people use, laid out by a real browser.

Whether Logout is on screen is a question for a layout engine, not for the stylesheet's text.
Each test renders the real base template with freshly compiled CSS and measures it in headless
Chrome. The pages inside the bar are checked by hand: see the FIX-021 notes in the audit report.

Skipped without Chrome or Jinja, or without the Tailwind binary the README's Development section
uses, which includes the test image. Runs on its own with
`pytest --noconftest tests/e2e/test_responsive_ui.py`.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

jinja2 = pytest.importorskip("jinja2")

ROOT = Path(__file__).resolve().parents[2]
TAILWIND = ROOT / "tools" / "tailwindcss"
CHROME = next(filter(None, map(shutil.which, ("google-chrome", "chromium"))), None)
SUBPROCESS_TIMEOUT_SECONDS = 60
LOG_RE = re.compile(r'<pre id="log">(.*?)</pre>', re.DOTALL)
STYLESHEET_RE = re.compile(r'href="[^"]*app\.css[^"]*"')
SUPPORTED_WIDTHS = (320, 390, 768, 1280)
TWO_ROW_WIDTHS = (320, 390)
LONG_NAME = "A Display Name That Goes On For Quite A While Indeed"

pytestmark = pytest.mark.skipif(
    CHROME is None or not TAILWIND.exists(), reason="needs Chrome and the Tailwind binary"
)

# Chrome will not make a headless window narrower than 500px, and media queries follow the
# frame they are in, so the page sits in an iframe of exactly the width under test.
FRAME = """<!doctype html><html><body><pre id="log"></pre>
<iframe srcdoc="{page}" style="width:{width}px;height:800px;border:0"></iframe>
</body></html>
"""
WIDE_WINDOW = "--window-size=1400,900"

MEASURE = """<script>
  function box(selector) {
    var rect = document.querySelector(selector).getBoundingClientRect();
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  }
  function ring(element) {
    element.focus();
    var style = getComputedStyle(element);
    return { style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor };
  }
  var skip = document.querySelector(".skip-link");
  var resting = skip.getBoundingClientRect().bottom;
  var result = {
    viewport: window.innerWidth,
    pageWidth: document.documentElement.scrollWidth,
    settings: box('.navlinks a[href$="/settings"]'),
    logout: box(".logout button"),
    brand: box(".brand"),
    navlinks: box(".navlinks"),
    position: getComputedStyle(document.querySelector(".topnav")).position,
    skipResting: resting,
    skipFirst: document.querySelector("a, button, input, select") === skip,
    linkRing: ring(document.querySelector('.navlinks a[href$="/calendar"]')),
    buttonRing: ring(document.querySelector(".logout button")),
    skipRing: ring(skip),
    skipTransition: getComputedStyle(skip).transform,
    skipFocused: skip.getBoundingClientRect().top,
    current: Array.prototype.map.call(
      document.querySelectorAll('[aria-current="page"]'),
      function (link) { return link.textContent; }
    ),
    navName: document.querySelector("nav").getAttribute("aria-label"),
    skipTarget: document.querySelector(skip.getAttribute("href")).tagName,
  };
  window.parent.document.getElementById("log").textContent = JSON.stringify(result);
</script>"""


@pytest.fixture(scope="module")
def stylesheet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiled = tmp_path_factory.mktemp("css") / "app.css"
    subprocess.run(  # noqa: S603 — the project's own Tailwind binary
        [
            str(TAILWIND), "-c", "tailwind.config.js",
            "-i", "styles/tailwind.css", "-o", str(compiled),
        ],
        cwd=ROOT, capture_output=True, timeout=SUBPROCESS_TIMEOUT_SECONDS, check=True,
    )
    return compiled


def _measure(tmp_path: Path, stylesheet: Path, width: int, *, theme: str = "dark") -> dict:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "app" / "templates"), autoescape=True
    )
    rendered = environment.get_template("base.html").render(
        current_user=SimpleNamespace(display_name=LONG_NAME),
        user_initials="AD",
        active_nav="settings",
        url_base="",
        brand="BoxMedia",
        asset_version="1",
        csrf_token="x",  # noqa: S106 — nothing here is ever submitted
        theme=theme,
    )
    with_css = STYLESHEET_RE.sub(f'href="{stylesheet.as_uri()}"', rendered, count=1)
    page = tmp_path / "page.html"
    measured = with_css.replace("</body>", MEASURE + "</body>")
    page.write_text(FRAME.format(page=html.escape(measured, quote=True), width=width), "utf-8")
    result = subprocess.run(  # noqa: S603 — our own browser, on a page written just above
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp_path / 'profile'}", WIDE_WINDOW,
            "--virtual-time-budget=1000", "--dump-dom", page.as_uri(),
        ],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS, check=True,
    )
    return json.loads(html.unescape(LOG_RE.search(result.stdout).group(1)))


@pytest.mark.parametrize("width", SUPPORTED_WIDTHS)
def test_the_page_never_scrolls_sideways_and_nothing_is_off_screen(
    tmp_path: Path, stylesheet: Path, width: int
) -> None:
    """The audit measured a 580px page at 390px, with Settings and Logout outside the view."""
    bar = _measure(tmp_path, stylesheet, width)

    assert bar["viewport"] == width
    assert bar["pageWidth"] <= width
    for control in ("settings", "logout", "brand"):
        assert 0 <= bar[control]["left"], control
        assert bar[control]["right"] <= width, control


@pytest.mark.parametrize("width", TWO_ROW_WIDTHS)
def test_a_phone_gets_the_links_on_their_own_row_and_no_sticky_bar(
    tmp_path: Path, stylesheet: Path, width: int
) -> None:
    bar = _measure(tmp_path, stylesheet, width)

    assert bar["navlinks"]["top"] >= bar["brand"]["bottom"]
    assert bar["position"] == "static"  # two sticky rows would hide too much of the screen


@pytest.mark.parametrize("width", TWO_ROW_WIDTHS)
def test_on_a_phone_the_keyboard_meets_things_where_the_eye_does(
    tmp_path: Path, stylesheet: Path, width: int
) -> None:
    """Tab goes brand, links, account, so that has to be the order they read in."""
    bar = _measure(tmp_path, stylesheet, width)

    assert bar["brand"]["bottom"] <= bar["navlinks"]["top"]
    assert bar["navlinks"]["bottom"] <= bar["logout"]["top"]


def test_a_wide_window_keeps_one_sticky_row(tmp_path: Path, stylesheet: Path) -> None:
    bar = _measure(tmp_path, stylesheet, 1280)

    assert bar["navlinks"]["top"] < bar["brand"]["bottom"]
    assert bar["position"] == "sticky"


def test_the_skip_link_comes_first_and_only_shows_when_focused(
    tmp_path: Path, stylesheet: Path
) -> None:
    bar = _measure(tmp_path, stylesheet, 390)

    assert bar["skipFirst"] is True
    assert bar["skipResting"] <= 0  # out of sight
    assert bar["skipTarget"] == "MAIN"


def test_the_current_page_and_the_navigation_are_named(tmp_path: Path, stylesheet: Path) -> None:
    bar = _measure(tmp_path, stylesheet, 1280)

    assert bar["current"] == ["Settings"]
    assert bar["navName"] == "Main"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_keyboard_focus_is_visible_in_both_themes(
    tmp_path: Path, stylesheet: Path, theme: str
) -> None:
    bar = _measure(tmp_path, stylesheet, 1280, theme=theme)

    for ring in (bar["linkRing"], bar["buttonRing"]):
        assert ring["style"] == "solid"
        assert ring["width"] == "2px"
        assert "rgba(0, 0, 0, 0)" not in ring["color"]
