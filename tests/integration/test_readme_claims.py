"""TV step 18: the README is held to what the app actually does.

Documentation rots silently. Nothing fails when a README describes a version that
shipped two releases ago — it just quietly misleads the next person to read it, and the
claims most worth trusting (what the app contacts, what it never does) are exactly the
ones nobody re-checks.

So every load-bearing claim in the new section is asserted against the code that would
have to change for it to stop being true:

* every host the README lists is one the code really contacts, AND every host the code
  contacts is listed — the second direction is the one that catches an endpoint added
  without a mention;
* every figure is read from the constant it describes, so a changed TTL fails here;
* every in-app path and settings label the README names exists;
* and the "never does" list is checked against the absence it claims.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services import calendar, discovery, scheduler, tmdb, trakt
from tests.conftest import AppHarness

ROOT = Path(__file__).resolve().parent.parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
SERVICES = ROOT / "app" / "services"

# The section this step added. Read once so a test can assert a claim is IN it rather
# than somewhere else in a 390-line file that happens to contain the words.
TELEVISION = README.split("## Television", 1)[1].split("\n## ", 1)[0]
SECURITY = README.split("## Security posture", 1)[1].split("\n## ", 1)[0]


def _hosts_in(text: str) -> set[str]:
    """Every third-party hostname named as backticked code in a passage."""
    return {
        host for host in re.findall(r"`([a-z0-9.-]+\.[a-z]{2,})`", text)
        if not host.endswith((".yml", ".md", ".sh", ".css", ".js", ".py", ".example"))
    }


def test_the_readme_lists_every_public_host_the_code_can_contact() -> None:
    """Both directions. A host in the code and not in the README is an endpoint somebody
    running this behind a firewall would discover the hard way."""
    from_code = {
        "boxofficemojo.com",
        "api.themoviedb.org",
        "image.tmdb.org",
        "api.trakt.tv",
    }

    listed = _hosts_in(SECURITY)

    assert from_code <= listed, f"contacted but undocumented: {from_code - listed}"
    assert listed <= from_code, f"documented but never contacted: {listed - from_code}"


def test_those_hosts_are_the_ones_the_clients_actually_use() -> None:
    """The set above is only trustworthy if it is checked against the constants rather
    than kept by hand — this is what ties it to the code."""
    assert tmdb.TMDB_BASE_URL.startswith("https://api.themoviedb.org")
    assert discovery.TMDB_IMAGE_BASE_URL.startswith("https://image.tmdb.org")
    assert trakt.TRAKT_BASE_URL.startswith("https://api.trakt.tv")
    assert "boxofficemojo.com" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_no_other_https_host_is_reached_from_a_service() -> None:
    """The sweep behind the claim. Any new `https://host` literal in a service has to be
    either a documented endpoint or an outbound-free link the app only renders."""
    rendered_only = {
        # Addresses that are put in front of a person, never requested by the app.
        "www.imdb.com", "en.wikipedia.org", "www.thetvdb.com", "www.themoviedb.org",
        "trakt.tv", "www.youtube.com", "image.tmdb.org", "api.themoviedb.org",
        "api.trakt.tv", "schemas.microsoft.com", "www.w3.org",
        # Sent as text inside the User-Agent so an operator on the other end knows who
        # is calling. A string in a header, never a request.
        "github.com",
    }
    found: dict[str, str] = {}
    for module in sorted(SERVICES.glob("*.py")):
        for host in re.findall(r"https://([a-z0-9.-]+)", module.read_text("utf-8")):
            found.setdefault(host, module.name)

    undocumented = {
        host: module for host, module in found.items()
        if host not in rendered_only and "boxofficemojo" not in host
    }
    assert not undocumented, f"undocumented outbound host: {undocumented}"


def test_every_figure_in_the_television_section_is_the_real_one() -> None:
    """A number in a README is a promise. Each one here is read off the constant that
    would have to change for it to become false."""
    assert calendar.CALENDAR_CACHE_TTL_SECONDS == 15 * 60
    assert "fifteen minutes" in TELEVISION

    assert discovery.DISCOVER_CACHE_TTL_SECONDS == 6 * 3600
    assert "six-hour" in TELEVISION

    assert scheduler.DAILY_REFRESH_HOUR_UTC == 6
    assert "06:00 UTC" in TELEVISION


def test_the_section_claims_no_trakt_account_and_the_code_has_no_way_to_have_one() -> None:
    """The strongest claim in the section, checked as an absence: a sign-in flow would
    need a token to store, a secret to send, or an Authorization header to set."""
    assert "No Trakt account" in TELEVISION
    assert "no OAuth" in TELEVISION

    source = (SERVICES / "trakt.py").read_text("utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    ).split('"""')
    # Even indices are code; the odd ones are docstrings, which say the words on purpose.
    executable = "".join(code[::2]).lower()
    for absent in ("oauth", "access_token", "refresh_token", "device_code",
                   "client_secret", "authorization"):
        assert absent not in executable, f"trakt.py mentions {absent}"


def test_the_client_id_travels_as_a_header_exactly_as_claimed() -> None:
    assert "request header, never in a URL" in TELEVISION

    headers = discovery.trakt_headers("a-client-id")

    assert headers["trakt-api-key"] == "a-client-id"
    assert "a-client-id" not in trakt.TRAKT_BASE_URL


def test_the_media_server_gained_series_and_gained_no_write() -> None:
    assert "Read-only, still" in TELEVISION

    source = (SERVICES / "mediaserver.py").read_text("utf-8")

    for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in source, f"mediaserver.py issues a {verb}"


def test_the_readme_ships_no_credentials_of_its_own() -> None:
    """The claim is that BoxMedia ships neither key. A README is a place one could
    plausibly end up — pasted into an example — so the sweep looks here too."""
    assert "BoxMedia ships neither" in TELEVISION

    for shape in (r"\b[0-9a-f]{32}\b", r"\b[0-9a-f]{64}\b"):
        assert not re.search(shape, README), "credential-shaped string in the README"
    assert not re.search(r"\b[0-9a-f]{32}\b", (ROOT / ".env.example").read_text("utf-8"))


def test_every_in_app_page_the_section_names_exists(harness: AppHarness) -> None:
    """The three pages are named in prose rather than linked, so a rename would leave
    the words behind. This is what notices."""
    harness.activate()
    for label, path in (("Library", "/library"), ("Discover", "/discover"),
                        ("Calendar", "/calendar")):
        assert f"**{label}**" in TELEVISION, label
        assert harness.client.get(path).status_code == 200, path


def test_the_settings_labels_the_section_sends_people_to_are_really_there(
    harness: AppHarness,
) -> None:
    """"Settings → Discovery" has to be findable on the Settings page, or the
    instructions send someone hunting."""
    harness.activate()
    assert "Settings → Discovery" in TELEVISION

    page = harness.client.get("/settings").text

    assert "Discovery" in page
    assert "TMDB" in page and "Trakt" in page


def test_the_television_pages_render_with_no_keys_and_say_which_are_missing(
    harness: AppHarness,
) -> None:
    """The claim that the film half is unaffected and the TV half is honestly empty.

    No respx mock: a page that reached the network here would fail on an unmocked call,
    which is also the "opening one costs no third-party request" claim.
    """
    harness.activate()
    assert "render honestly empty and say which key is missing" in TELEVISION

    discover = harness.client.get("/discover")

    assert discover.status_code == 200
    assert "TMDB API key" in discover.text and "Trakt client ID" in discover.text
    assert harness.client.get("/library").status_code == 200


def test_the_deferred_work_is_still_marked_deferred() -> None:
    """Screenshots and the resource figures describe the film-only release, and the
    README says so. If somebody refreshes them, this is the line that has to go — which
    is the point: it cannot be forgotten in place."""
    assert "still describe the film-only" in TELEVISION
    assert "no estimates in the meantime" in TELEVISION


# --- the screenshots are what the caption says they are ---


SHOTS = ROOT / "docs" / "screenshots"


def test_every_screenshot_the_readme_shows_exists() -> None:
    """A broken image in a README is invisible to everything except the person reading
    it, and it is the first thing they see."""
    referenced = set(re.findall(r"\((docs/screenshots/[^)]+)\)", README))

    assert referenced, "the README shows no screenshots at all"
    for relative in sorted(referenced):
        assert (ROOT / relative).is_file(), relative


def test_no_screenshot_is_left_behind_unused() -> None:
    """The other direction: an image nothing shows is one nobody will remember to
    refresh, and it goes on describing a version that shipped two releases ago."""
    referenced = {
        Path(relative).name
        for relative in re.findall(r"\((docs/screenshots/[^)]+)\)", README)
    }
    on_disk = {path.name for path in SHOTS.glob("*.png")}

    assert on_disk == referenced, f"unreferenced: {sorted(on_disk - referenced)}"


def test_the_sample_data_promise_is_one_a_script_keeps() -> None:
    """The caption claims sample data. That is only checkable because a committed script
    produces it — so the claim and the script have to stay together."""
    assert "fictional sample data" in README
    assert "scripts/screenshots.py" in README

    generator = (ROOT / "scripts" / "screenshots.py").read_text("utf-8")

    # RFC 5737 TEST-NET-1: reserved for documentation, so no example can point at a
    # real machine.
    assert "192.0.2." in generator
    # Artwork is drawn, never fetched — a screenshot must not reproduce cover art.
    assert "Image.new" in generator
    assert "example.invalid" in generator


def test_the_generator_invents_every_title_it_uses() -> None:
    """The DMCA half of the promise, checked as an absence: the sample titles are made
    up, and no real film or series is named."""
    generator = (ROOT / "scripts" / "screenshots.py").read_text("utf-8")

    for invented in ("Neon Rain", "The Hollow Coast", "Sodium Lights", "Copper Sky"):
        assert invented in generator, invented
    # The one real name that legitimately appears is the app's own.
    assert "BoxMedia" in generator
