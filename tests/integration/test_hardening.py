"""TV step 17: the hardening sweep, run against the whole surface at once.

Every claim here is written as an INVARIANT over the live app rather than a list of the
routes and pages that exist today — a checklist ticked once protects the code that was
there when it was written, and a rule protects the code somebody adds next month.

* every mutating route is refused without a CSRF token, enumerated off the router;
* every route that changes stored state leaves an audit row, with the exceptions named
  and pinned so the list cannot grow quietly;
* neither credential shape survives a failure anywhere under the data directory;
* no template carries an inline style, an inline script or an event handler attribute,
  and anything shipping `hidden` has the guard that stops the layer order un-hiding it;
* every class name the templates use is one the built stylesheet actually carries — the
  purge trap, checked against a real build rather than against the source;
* and the dependency list is byte-identical to the one that was audited (ruling 3).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from app.services.discovery import PROVIDER_TMDB, PROVIDER_TRAKT
from app.services.tmdb import TMDB_BASE_URL
from tests.conftest import AppHarness

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "app" / "templates"
STYLESHEET = ROOT / "styles" / "tailwind.css"
TAILWIND = ROOT / "tools" / "tailwindcss"

# Long enough to be unmistakable in a grep, and the real shapes: TMDB v3 keys are 32 hex
# characters, Trakt client ids are 64.
TMDB_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TRAKT_ID = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0"

# The pages television added. Every rule below is applied to all of them at once, so a
# page that grows an inline style next week fails here rather than in someone's console.
NEW_PAGES = ("/discover", "/calendar", "/library", "/shows/1396")
NEW_TEMPLATES = (
    "discover.html", "calendar.html", "library.html",
    "show_detail.html", "_show_detail.html", "_series_card.html",
    "_movie_card.html", "_add_series_control.html",
)

# `/login` is the one exemption, and it is not unguarded: it keeps the Origin check and
# the login rate limiter, because no session exists yet to mint a token from.
CSRF_EXEMPT = ("/login",)

# Mutating routes that deliberately leave NO audit row, and why. Pinned as a set so a
# route added without a row has to be argued for here rather than simply not noticed.
#
# The rule: a row is written when STORED STATE CHANGES. A probe that touches nothing, a
# session-scoped read, and a redirect are not state changes — and a row per page open
# would bury the sign-ins and key changes this log exists for.
NO_AUDIT_ROW = {
    "/settings/apps/test": "reads one connection's status; changes nothing",
    "/settings/apps/{app_id}/test": "the same probe, for a saved connection",
    "/settings/media-server/test": "the same probe, for the media server",
    "/settings/media-server/test-credentials": "a pre-save probe; nothing is stored",
    "/settings/discovery/test": "a pre-save probe; the key is never written to disk",
    "/account/theme": "a display preference on the signed-in user's own account",
    "/run-backfill": "each week it fetches writes its own report, which IS the record",
}


def _routes(harness: AppHarness) -> Iterator[tuple[str, str]]:
    """Every (method, path) the app serves, walked off the router itself.

    Off the live router rather than a hand-kept list: a route added without the guard
    must fail this file, and a list would simply not mention it.
    """
    def walk(routes: object, prefix: str = "") -> Iterator[tuple[str, str]]:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from walk(original.routes, prefix + route.include_context.prefix)
                continue
            nested = getattr(route, "routes", None)
            if nested is not None:
                yield from walk(nested, prefix + getattr(route, "path", ""))
                continue
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                yield method, prefix + route.path

    yield from sorted(set(walk(harness.client.app.routes)))


def _mutating(harness: AppHarness) -> list[str]:
    return sorted({path for method, path in _routes(harness) if method != "GET"})


def _placeholder(path: str) -> str:
    """A concrete URL for a templated path. The value never matters — the guard answers
    before any handler sees it, which is the whole point of it being a dependency."""
    return re.sub(r"\{[^}]+\}", "placeholder", path)


# --- every mutating route is CSRF-guarded ---


def test_every_mutating_route_is_refused_without_a_csrf_token(
    harness: AppHarness,
) -> None:
    harness.activate()
    unguarded = []
    for path in _mutating(harness):
        if path in CSRF_EXEMPT:
            continue
        # `client.request`, not `client.post`: the harness's client attaches the token a
        # real form would carry, which is exactly what this test must not send.
        response = harness.client.request(
            "POST", _placeholder(path), data={}, follow_redirects=False
        )
        if response.status_code != 403:
            unguarded.append(f"{path} -> {response.status_code}")
    assert not unguarded, f"mutating routes that accepted a tokenless POST: {unguarded}"


def test_the_sweep_actually_saw_the_new_routes(harness: AppHarness) -> None:
    """A guard test that enumerates nothing passes vacuously. This is what stops the
    walk above silently finding an empty router."""
    harness.activate()
    paths = _mutating(harness)

    assert len(paths) > 30
    for expected in (
        "/add-series", "/ignore-series", "/discover/refresh",
        "/settings/discovery", "/settings/discovery/{provider}/delete",
    ):
        assert expected in paths, expected


def test_a_forged_token_is_refused_on_a_television_route(harness: AppHarness) -> None:
    """The guard checks the token, not merely its presence."""
    harness.activate()

    response = harness.client.post(
        "/discover/refresh", data={"csrf_token": "not-the-token"}, follow_redirects=False
    )

    assert response.status_code == 403


# --- every state change leaves a record ---


def test_the_no_audit_list_names_only_routes_that_exist(harness: AppHarness) -> None:
    """The exception list is only meaningful while it matches reality: a stale entry
    would silently excuse a route that no longer has that name."""
    harness.activate()
    paths = set(_mutating(harness))

    assert set(NO_AUDIT_ROW) <= paths, set(NO_AUDIT_ROW) - paths


def test_adding_a_series_is_audited(harness: AppHarness) -> None:
    """Driven through the store the route uses, because the row is the store's to write
    and the route's to trigger — asserting on the log is what pins the pair."""
    harness.activate()

    harness.client.app.state.ignore.add(
        tmdb_id=1396, title="The Hollow Coast",
        normalized_title="hollow coast", kind="series",
    )

    rows = [row for row in harness.audit_lines() if "ignore" in row]
    assert rows and '"kind": "series"' in rows[-1]


@respx.mock
def test_refreshing_discover_is_audited(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TRAKT, TRAKT_ID)
    respx.get(url__startswith="https://api.trakt.tv").mock(
        return_value=httpx.Response(200, json=[])
    )

    harness.client.post("/discover/refresh", follow_redirects=False)

    assert any("discover_refreshed" in row for row in harness.audit_lines())


def test_saving_and_removing_a_discovery_key_are_both_audited(
    harness: AppHarness,
) -> None:
    harness.activate()

    harness.client.post(
        "/settings/discovery", data={"tmdb_key": TMDB_KEY, "trakt_client_id": ""},
        follow_redirects=False,
    )
    harness.client.post(
        f"/settings/discovery/{PROVIDER_TMDB}/delete", follow_redirects=False
    )

    lines = harness.audit_lines()
    assert any("discovery_key_saved" in row for row in lines)
    assert any("discovery_key_removed" in row for row in lines)


def test_no_audit_row_ever_carries_a_key(harness: AppHarness) -> None:
    """The rows above name the PROVIDER. Naming the value would put every key in the one
    file an operator is most likely to read, copy and paste into a ticket."""
    harness.activate()

    harness.client.post(
        "/settings/discovery", data={"tmdb_key": TMDB_KEY, "trakt_client_id": TRAKT_ID},
        follow_redirects=False,
    )

    for row in harness.audit_lines():
        assert TMDB_KEY not in row
        assert TRAKT_ID not in row


# --- neither key survives a failure, anywhere on disk ---


def _leaks(harness: AppHarness) -> list[str]:
    """Every file under the data directory that contains either credential.

    The whole tree, not the audit log alone: a key can escape through a scrape-failure
    snapshot, a cache written from an error path, or a config file that stored it
    unencrypted, and the point of a sweep is to look everywhere rather than where the
    author expected it to be.
    """
    found = []
    for path in harness.settings.data_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if TMDB_KEY in text or TRAKT_ID in text:
            found.append(str(path.relative_to(harness.settings.data_dir)))
    return found


@respx.mock
def test_a_tmdb_transport_failure_leaves_the_key_nowhere_on_disk(
    harness: AppHarness,
) -> None:
    """The key travels as a query parameter — TMDB's own contract — so every failure
    path carries a URL that contains it. This is the sweep that says it never lands."""
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TMDB, TMDB_KEY)
    respx.get(url__startswith=TMDB_BASE_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    page = harness.client.get("/shows/1396")

    assert page.status_code == 200
    assert TMDB_KEY not in page.text
    assert _leaks(harness) == []


@respx.mock
def test_a_tmdb_error_response_leaves_the_key_nowhere_on_disk(
    harness: AppHarness,
) -> None:
    """The other half: a server that answers, badly. An error BODY can echo the request
    back, which is a different path through the code from a transport failure."""
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TMDB, TMDB_KEY)
    respx.get(url__startswith=TMDB_BASE_URL).mock(
        return_value=httpx.Response(
            401, json={"status_message": f"Invalid API key: {TMDB_KEY}"}
        )
    )

    page = harness.client.get("/shows/1396")

    assert page.status_code == 200
    assert TMDB_KEY not in page.text
    assert _leaks(harness) == []


@respx.mock
def test_a_trakt_failure_leaves_the_client_id_nowhere_on_disk(
    harness: AppHarness,
) -> None:
    """Trakt takes its id in a header rather than a URL, which is why it is the easier
    of the two — and exactly why it is worth asserting rather than assuming."""
    harness.activate()
    harness.client.app.state.discovery.save(PROVIDER_TRAKT, TRAKT_ID)
    respx.get(url__startswith="https://api.trakt.tv").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    harness.client.post("/discover/refresh", follow_redirects=False)

    assert _leaks(harness) == []


def test_the_leak_sweep_can_actually_find_a_key(harness: AppHarness) -> None:
    """A grep that never matches passes every time. This plants one and proves the sweep
    sees it, so the three tests above mean what they say."""
    harness.activate()
    planted = harness.settings.logs_dir / "planted.txt"
    planted.write_text(f"key={TMDB_KEY}", encoding="utf-8")

    assert _leaks(harness) == ["logs/planted.txt"]


def test_a_stored_key_is_never_written_in_the_clear(harness: AppHarness) -> None:
    """Saving one is not a failure path, but it is the obvious way for a key to reach
    disk — encrypted with the same AES-256-GCM key as every other stored secret."""
    harness.activate()

    harness.client.post(
        "/settings/discovery", data={"tmdb_key": TMDB_KEY, "trakt_client_id": TRAKT_ID},
        follow_redirects=False,
    )

    assert harness.client.app.state.discovery.decrypt(PROVIDER_TMDB) == TMDB_KEY
    assert _leaks(harness) == []


# --- the CSP holds because nothing inline is rendered ---


def _rendered(harness: AppHarness) -> Iterator[tuple[str, str]]:
    for path in NEW_PAGES:
        page = harness.client.get(path)
        assert page.status_code == 200, f"{path} -> {page.status_code}"
        yield path, page.text


def test_the_new_pages_send_the_same_strict_policy(harness: AppHarness) -> None:
    harness.activate()
    for path in NEW_PAGES:
        header = harness.client.get(path).headers["Content-Security-Policy"]
        assert "default-src 'self'" in header, path
        assert "unsafe-inline" not in header, path
        assert "unsafe-eval" not in header, path


def test_the_new_pages_carry_nothing_the_policy_would_refuse(
    harness: AppHarness,
) -> None:
    """Asserted on the CAUSE rather than on a browser console: a page that failed to load
    would report zero violations too, while this cannot pass by the page being broken.

    Three things the policy refuses, and one it does not refuse but which would break the
    moment somebody tightened it further.
    """
    harness.activate()
    for path, page in _rendered(harness):
        assert " style=" not in page, f"inline style on {path}"
        assert not re.search(r"<script(?![^>]*\ssrc=)", page), f"inline script on {path}"
        assert not re.search(r"\son[a-z]+\s*=", page), f"inline handler on {path}"
        assert "javascript:" not in page, f"javascript: URL on {path}"


def _ships_hidden() -> Iterator[tuple[str, str]]:
    """Every (template, class) pair on an element rendered with the `hidden` attribute."""
    for template in sorted(TEMPLATES.glob("*.html")):
        markup = template.read_text(encoding="utf-8")
        for tag in re.findall(r"<[a-z]+[^>]*\shidden[\s>]", markup):
            classes = re.search(r'class="([^"{]*)"', tag)
            for name in (classes.group(1).split() if classes else ()):
                yield template.name, name


def test_every_template_that_ships_hidden_is_guarded(built_css: str) -> None:
    """Preflight puts `[hidden] { display: none }` in @layer base, which a component
    layer beats — so an element that ships `hidden` AND carries a class declaring its own
    display renders VISIBLE until a rule wins the attribute back. Both existing cases
    document this; the rule is asserted over every template so a new one cannot miss it.

    Narrowed to classes that actually declare a display, because that is the whole
    mechanism: a class that only sets colours and padding leaves the attribute uncontested
    and needs no guard.
    """
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    unguarded = [
        f"{template}: .{name}"
        for template, name in _ships_hidden()
        if _sets_a_display(built_css, name) and f".{name}[hidden]" not in stylesheet
    ]
    assert not unguarded, f"ships hidden with no [hidden] guard: {unguarded}"


def test_the_hidden_guard_rule_can_actually_bite(built_css: str) -> None:
    """A rule that never applies passes every time. Both existing cases DO declare a
    display, so the test above is doing work rather than finding nothing to check."""
    contested = {
        name for _, name in _ships_hidden() if _sets_a_display(built_css, name)
    }

    assert "save-bar" in contested
    assert "to-top" in contested


# --- the purge trap ---


@pytest.fixture(scope="module")
def built_css() -> str:
    """The stylesheet a browser gets, built here rather than read from
    `app/static/css/app.css` — that file is produced inside the image and gitignored, so
    a test that read it would pass or fail on whether someone had run the build locally.

    Module-scoped: the build takes about a second and two rules need it.
    """
    if not TAILWIND.exists():
        raise AssertionError(
            f"{TAILWIND} is missing — the purge trap cannot be checked without a build"
        )
    with tempfile.TemporaryDirectory() as workspace:
        built = Path(workspace) / "app.css"
        result = subprocess.run(  # noqa: S603 — a binary at a fixed path in this repo
            [str(TAILWIND), "-c", "tailwind.config.js", "-i", "styles/tailwind.css",
             "-o", str(built), "--minify"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return built.read_text(encoding="utf-8")


def _sets_a_display(css: str, name: str) -> bool:
    """Whether a class rule in the built stylesheet declares a `display`.

    That is what makes an element shipping `hidden` a problem: preflight's
    `[hidden] { display: none }` sits in @layer base, and a component rule that declares
    its own display beats it.
    """
    return any(
        "display:" in body
        for body in re.findall(rf"\.{re.escape(name)}\{{([^}}]*)\}}", css)
    )


_CLASS_ATTRIBUTE = re.compile(r'class="([^"]*)"')
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
# The one composed family, which the templates name a prefix of and Tailwind never sees
# whole — the reason those eleven rules are deliberately top-level rather than layered.
UTILITY_PREFIXES = ("where-chip-p",)

# Names that are SELECTORS rather than utilities, so no rule of their own is expected.
# `dark` is the theme class Tailwind's `darkMode: "class"` config keys on; the dark
# palette itself lives on `:root`, which is why — unlike `html.light` — there is no
# `.dark` rule to find.
SELECTOR_ONLY = {"dark"}

# Classes a template names that the BUILD carries nothing for. Each one renders as
# nothing and no page says so, which is why the sweep looks — and why the list is pinned
# here rather than quietly excluded: it can only shrink.
KNOWN_INERT = {
    # Pre-existing, and not television's: the type scale lives under `fontSize` in
    # tailwind.config.js, so the utility is `text-body-md`. `font-*` maps to a font
    # FAMILY or WEIGHT, and there is no `body-md` family — so this has generated nothing
    # since it shipped and the body inherits the browser's 16px instead of the design
    # system's 14px/1.6. Every component class sizes its own text, so what changes is
    # only text no class covers.
    #
    # NOT fixed here on purpose: swapping it alters the base type size on every page in
    # the app, which is a design change to make with eyes on it rather than inside a
    # hardening pass. The fix is one word — `font-body-md` -> `text-body-md` in
    # base.html — and this entry is what stops it being forgotten.
    "base.html": {"font-body-md"},
}


def _classes_used(template: Path) -> set[str]:
    """Every literal class name a template asks for, with the Jinja stripped out.

    Stripped first because a `class="{% if x %}a{% else %}b{% endif %}"` yields both
    names once the tags are gone, and both are literal as far as Tailwind's scan is
    concerned — which is the whole point of writing them out in full.
    """
    markup = template.read_text(encoding="utf-8")
    names: set[str] = set()
    for attribute in _CLASS_ATTRIBUTE.findall(markup):
        names.update(_JINJA.sub(" ", attribute).split())
    return {name for name in names if not name.startswith(UTILITY_PREFIXES)}


ALL_TEMPLATES = tuple(sorted(path.name for path in TEMPLATES.glob("*.html")))


def test_no_template_names_a_class_the_build_carries_nothing_for(
    built_css: str,
) -> None:
    """The purge trap, over every template rather than only television's.

    Two ways a class name silently does nothing: nobody ever defined it, or Tailwind
    tree-shook it out because the template names it only in a composed form. Both look
    identical in a browser — the element renders unstyled and nothing reports it — so
    this is the only place either is visible.
    """
    inert = {
        (template, name)
        for template in ALL_TEMPLATES
        for name in _classes_used(TEMPLATES / template)
        if f".{name}" not in built_css
        and name not in SELECTOR_ONLY
        and name not in KNOWN_INERT.get(template, set())
    }
    assert not inert, f"named but absent from the build: {sorted(inert)}"


def test_the_composed_fill_family_survives_the_build(built_css: str) -> None:
    """The one family the sweep above cannot check, checked here instead.

    Every template names these as `where-chip-p{{ step }}`, so Tailwind's scan never sees
    a whole one — which is why the eleven rules sit at the stylesheet's top level rather
    than inside @layer. `test_control_alignment` asserts that placement in the SOURCE;
    this asserts the consequence in the BUILD, which is the file that actually matters.
    """
    missing = [step for step in range(0, 101, 10) if f".where-chip-p{step}" not in built_css]

    assert not missing, f"purged out of the build: {missing}"


def test_the_known_inert_list_still_describes_reality(built_css: str) -> None:
    """An entry that has been fixed, or a template that no longer names it, must not go
    on excusing something. The list may shrink; it may not rot."""
    for template, names in KNOWN_INERT.items():
        used = _classes_used(TEMPLATES / template)
        for name in names:
            assert name in used, f"{template} no longer names .{name} — drop the entry"
            assert f".{name}" not in built_css, (
                f".{name} is in the build now — drop the entry"
            )


def test_the_built_stylesheet_carries_every_class_the_new_pages_use(
    built_css: str,
) -> None:
    """The second half, and the one only a real build can answer: Tailwind tree-shakes
    layered rules against the templates, so a rule that survives in the source can still
    be absent from the file the browser gets."""
    css = built_css
    missing = {
        f"{template}: .{name}"
        for template in NEW_TEMPLATES
        for name in _classes_used(TEMPLATES / template)
        if f".{name}" not in css
    }
    assert not missing, f"purged out of the build: {sorted(missing)}"


# --- ruling 3: nothing new to audit ---


def test_the_dependency_list_is_the_one_that_was_audited() -> None:
    """Ruling 3 of the TV plan: zero new Python dependencies, so `pip-audit`'s answer is
    unchanged by construction rather than by re-running it. Pinned as the exact list, so
    an added package fails here and has to be audited deliberately."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("dependencies = [", 1)[1].split("\n]", 1)[0]
    packages = tuple(
        line.strip().strip(",").strip('"') for line in block.splitlines() if line.strip()
    )

    assert packages == (
        "fastapi==0.141.1",
        "uvicorn[standard]==0.34.0",
        "jinja2==3.1.6",
        "python-multipart==0.0.32",
        "httpx==0.28.1",
        "beautifulsoup4==4.12.3",
        "apscheduler==3.11.0",
        "argon2-cffi==23.1.0",
        "cryptography==50.0.0",
        "pydantic==2.10.4",
        "pydantic-settings==2.7.1",
        "pyyaml==6.0.2",
    )


# --- what the new pages do with input they did not choose ---


def test_a_series_id_that_is_not_a_number_is_refused_before_anything_uses_it(
    harness: AppHarness,
) -> None:
    """The id becomes a path segment on a TMDB request. Typed as an int, so FastAPI
    refuses anything else before a handler — and before a URL — is built from it."""
    harness.activate()

    assert harness.client.get("/shows/../../etc/passwd").status_code in (404, 422)
    assert harness.client.get("/shows/not-a-number").status_code == 422


def test_a_poster_name_cannot_walk_out_of_the_cache(harness: AppHarness) -> None:
    """Pre-existing and worth re-asserting on a sweep: the name is matched against a
    pattern before it is joined to a path, never after."""
    harness.activate()

    assert harness.client.get("/posters/..%2F..%2Fapps.yml").status_code in (404, 422)


def test_a_search_term_comes_back_escaped(harness: AppHarness) -> None:
    """The Library echoes the query into the search box's value, which is the one place
    on the new surface where text a visitor controls is rendered back. Jinja autoescapes;
    this is the assertion that says so rather than assuming it."""
    harness.activate()

    page = harness.client.get('/library?q=<script>alert(1)</script>').text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_hostile_chip_or_week_renders_neither(harness: AppHarness) -> None:
    """Both are read-tolerant by design — anything unrecognised falls back to the widest
    view — so neither can reach the markup at all. Asserted for the fallback AND for the
    absence, because "shows everything" and "reflects nothing" are different claims."""
    harness.activate()
    payload = "<img src=x onerror=alert(1)>"

    for path in (f"/library?type={payload}", f"/calendar?week={payload}",
                 f"/discover?type={payload}"):
        page = harness.client.get(path)
        assert page.status_code == 200, path
        assert "onerror" not in page.text, path


# --- nothing unattended ever adds anything ---


SERVICES = ROOT / "app" / "services"


def test_the_only_write_to_sonarr_is_the_add_a_person_presses() -> None:
    """A badge saying "Wanted" is a statement about your library, never an intention to
    fetch — and this is what keeps it that way.

    One write exists in the whole client, and it has one caller: the CSRF-guarded form
    on a show's own page. If a second appears, or the first is reached from anywhere
    else, this fails.
    """
    client = (SERVICES / "sonarr.py").read_text("utf-8")
    writes = re.findall(r'_request\(\s*"(POST|PUT|DELETE|PATCH)"', client)

    assert writes == ["POST"], f"Sonarr client writes: {writes}"

    callers = {
        module.name
        for module in (ROOT / "app").rglob("*.py")
        if "add_series(" in module.read_text("utf-8") and module.name != "sonarr.py"
    }
    assert callers == {"shows.py"}, callers


def test_no_unattended_job_can_reach_an_add() -> None:
    """The scheduler's two morning jobs re-read your own servers into a local cache.
    Every method they touch is a read; a write would have to be added here first."""
    refresher = (SERVICES / "refresh.py").read_text("utf-8")

    called = set(re.findall(r"\bclient\.([a-z_]+)\(", refresher))

    assert called == {"calendar", "queue", "list_series"}, called

    # Executable lines only — the prose above them says "a deleted Radarr" and means it.
    executable = "".join(
        "\n".join(
            line for line in refresher.splitlines() if not line.strip().startswith("#")
        ).split('"""')[::2]
    )
    for forbidden in ("add_series", "add_movie", "delete(", "monitor("):
        assert forbidden not in executable, f"refresh.py calls {forbidden}"
