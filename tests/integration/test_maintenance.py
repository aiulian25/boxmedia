"""F17: the Storage card and the two maintenance routes."""

from __future__ import annotations

from app.core.audit import AuditAction
from app.services.boxoffice import SNAPSHOT_PREFIX, SNAPSHOT_SUFFIX
from app.services.posters import POSTER_SUBDIR
from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.web.settings import SettingsStatus
from tests.conftest import AppHarness

POSTER_URL = "http://radarr.local/MediaCover/1/poster.jpg"
ORPHAN_URL = "http://radarr.local/MediaCover/99/poster.jpg"
JPEG = b"\xff\xd8\xff jpeg"
# What `_snapshot_failure` writes: the byte length, then an 8-char content digest.
SNAPSHOT_NAME = f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}"
FAILED_PAGE = (
    "<html><script>alert(1)</script>failed page</html>" + "<p>filler</p>" * 150
)


def _seed_report_with_poster(harness: AppHarness) -> None:
    movie = MovieResult(
        rank=1, title="Neon Rain", normalized_title="neon rain", gross_amount=45_000_000,
        gross_display="$45.0M", weeks_in_release=1, status=MovieStatus.WANTED,
        action=MovieAction.NONE, tmdb_id=555, poster_url=POSTER_URL,
    )
    harness.client.app.state.reports.save(Report(
        id="report-20260814-100000-aaaa", run_at="2026-08-14T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W27",
        totals=ReportTotals(movies=1, matched=1), movies=[movie],
    ))


def _write_poster(harness: AppHarness, url: str):  # noqa: ANN202
    settings = harness.client.app.state.settings
    cache = harness.client.app.state.posters
    path = settings.cache_dir / POSTER_SUBDIR / cache.local_name(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    return path


def _write_snapshot(harness: AppHarness, *, name: str = SNAPSHOT_NAME, body: str = FAILED_PAGE):
    """A snapshot named the way `_snapshot_failure` actually names one.

    The old fixture wrote `scrape-failure-42.html`, from before the content digest joined
    the name — a shape the scraper has not produced for some time, and one the download
    route rightly refuses.
    """
    settings = harness.client.app.state.settings
    path = settings.logs_dir / "scrape-failures" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_storage_card_lists_every_data_area(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get("/settings").text
    assert "Storage" in page
    for label in ("Posters", "Scrape snapshots", "Report history", "Backups"):
        assert label in page
    assert "KB" in page  # sizes render human-readable, not raw bytes


def test_prune_drops_orphans_and_keeps_referenced_posters(harness: AppHarness) -> None:
    harness.activate()
    _seed_report_with_poster(harness)
    referenced = _write_poster(harness, POSTER_URL)
    orphan = _write_poster(harness, ORPHAN_URL)

    response = harness.client.post(
        "/settings/maintenance/prune-posters", follow_redirects=False
    )
    assert SettingsStatus.CLEANED in response.headers["location"]
    assert referenced.exists()  # a kept report still shows it
    assert not orphan.exists()


def test_clear_snapshots_empties_the_directory(harness: AppHarness) -> None:
    harness.activate()
    snapshot = _write_snapshot(harness)

    response = harness.client.post(
        "/settings/maintenance/clear-snapshots", follow_redirects=False
    )
    assert SettingsStatus.CLEANED in response.headers["location"]
    assert not snapshot.exists()


def test_cleanup_banner_renders_after_the_redirect(harness: AppHarness) -> None:
    harness.activate()
    page = harness.client.get(f"/settings?status={SettingsStatus.CLEANED}").text
    assert "Cleanup complete." in page


def test_maintenance_is_audited(harness: AppHarness) -> None:
    harness.activate()
    _seed_report_with_poster(harness)
    _write_poster(harness, ORPHAN_URL)
    harness.client.post("/settings/maintenance/prune-posters", follow_redirects=False)

    entries = harness.client.app.state.audit.tail(20)
    pruned = [e for e in entries if e["action"] == AuditAction.MAINTENANCE_PRUNE]
    assert pruned and pruned[-1]["target"] == "posters"
    assert pruned[-1]["removed"] == 1


def test_maintenance_requires_a_csrf_token(harness: AppHarness) -> None:
    harness.activate()
    orphan = _write_poster(harness, ORPHAN_URL)
    # .request() bypasses the harness's auto-CSRF, so this is a genuine tokenless POST.
    response = harness.client.request(
        "POST", "/settings/maintenance/prune-posters", follow_redirects=False
    )
    assert response.status_code == 403
    assert orphan.exists()  # nothing deleted


def test_maintenance_requires_a_session(harness: AppHarness) -> None:
    harness.activate()
    orphan = _write_poster(harness, ORPHAN_URL)
    harness.client.cookies.clear()
    response = harness.client.post(
        "/settings/maintenance/prune-posters", follow_redirects=False
    )
    # A success also redirects, so assert it is NOT the cleanup redirect.
    assert SettingsStatus.CLEANED not in response.headers.get("location", "")
    assert orphan.exists()


TMDB_POSTER = "https://image.tmdb.org/t/p/original/xyz.jpg"


def _seed_report_with_tmdb_poster(harness: AppHarness) -> None:
    movie = MovieResult(
        rank=1, title="Sized", normalized_title="sized", gross_amount=1,
        gross_display="$1", weeks_in_release=1, status=MovieStatus.WANTED,
        action=MovieAction.NONE, tmdb_id=1, poster_url=TMDB_POSTER,
    )
    harness.client.app.state.reports.save(Report(
        id="report-20260814-120000-size", run_at="2026-08-14T12:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week="2026W27",
        totals=ReportTotals(movies=1, matched=1), movies=[movie],
    ))


def test_prune_keeps_a_poster_the_app_just_cached(harness: AppHarness) -> None:
    """The drift guard: the fetch and the keep-set must agree on the URL form.

    Caching rewrites the TMDB size; if prune built its keep-set from the raw stored URL
    it would treat every cached poster as an orphan and wipe the cache.
    """
    from app.services.posters import POSTER_WIDTH, sized

    harness.activate()
    _seed_report_with_tmdb_poster(harness)
    cached = _write_poster(harness, sized(TMDB_POSTER, POSTER_WIDTH))
    assert cached.exists()

    harness.client.post("/settings/maintenance/prune-posters", follow_redirects=False)
    assert cached.exists()  # still referenced by a retained report


# --- the evidence is readable where it is created (F15) ---


def test_the_card_lists_a_stored_snapshot_with_its_size_and_date(
    harness: AppHarness,
) -> None:
    """Write-only until now: the scraper saves the page it could not parse "for
    diagnosis", and this runtime has no shell to open it with."""
    harness.activate()
    _write_snapshot(harness)

    page = harness.client.get("/settings").text

    assert "Scrape failure snapshots" in page
    assert SNAPSHOT_NAME in page
    assert f"/settings/maintenance/snapshots/{SNAPSHOT_NAME}" in page
    assert "2.0 KB" in page  # _size_display, the same one the Storage table uses


def test_the_card_says_so_when_there_is_nothing_to_read(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/settings").text

    assert "No scrape failures recorded." in page
    assert "/settings/maintenance/snapshots/" not in page


def test_a_snapshot_downloads_as_plain_text_never_as_html(harness: AppHarness) -> None:
    """The whole security question in one assertion. The file is a page from somewhere
    else — a Mojo layout change, or whatever a proxy in front of it answered with. Served
    as text/html it would run its scripts in BoxMedia's own origin, with this session's
    cookie attached.
    """
    harness.activate()
    _write_snapshot(harness)

    response = harness.client.get(f"/settings/maintenance/snapshots/{SNAPSHOT_NAME}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "html" not in response.headers["content-type"]
    # An attachment, so a browser saves it rather than navigating to it...
    assert "attachment" in response.headers["content-disposition"]
    # ...and the type cannot be second-guessed even so.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<script>alert(1)</script>" in response.text  # the evidence arrives intact


def test_a_crafted_snapshot_name_is_not_found(harness: AppHarness) -> None:
    """The request contributes a NAME, never a path segment: the allow-list's alphabet has
    no separator in it, so a traversal is unrepresentable rather than merely rejected."""
    harness.activate()
    _write_snapshot(harness)
    settings = harness.client.app.state.settings
    (settings.logs_dir / "audit.jsonl").write_text("secret", encoding="utf-8")

    for crafted in (
        "../audit.jsonl",
        "..%2Faudit.jsonl",
        "x.html",
        f"{SNAPSHOT_PREFIX}1024-A1B2C3D4{SNAPSHOT_SUFFIX}",   # digest must be lowercase
        f"{SNAPSHOT_PREFIX}1024-a1b2c3d{SNAPSHOT_SUFFIX}",    # ...and eight characters
        f"{SNAPSHOT_PREFIX}abc-a1b2c3d4{SNAPSHOT_SUFFIX}",    # the length must be digits
    ):
        response = harness.client.get(
            f"/settings/maintenance/snapshots/{crafted}", follow_redirects=False
        )
        assert response.status_code in (404, 307), crafted
        assert "secret" not in response.text, crafted


def test_only_files_this_app_wrote_are_listed(harness: AppHarness) -> None:
    """The directory is under /data/logs, which the admin can put anything into. The card
    lists what BoxMedia produced, and nothing else."""
    harness.activate()
    _write_snapshot(harness)
    settings = harness.client.app.state.settings
    stray = settings.logs_dir / "scrape-failures" / "notes-from-the-admin.txt"
    stray.write_text("mine, not yours", encoding="utf-8")
    legacy = settings.logs_dir / "scrape-failures" / f"{SNAPSHOT_PREFIX}42{SNAPSHOT_SUFFIX}"
    legacy.write_text("from before the digest was in the name", encoding="utf-8")

    page = harness.client.get("/settings").text

    assert SNAPSHOT_NAME in page
    assert "notes-from-the-admin.txt" not in page
    # Not listed, because it could not be downloaded either — but clearing still sweeps it.
    assert legacy.name not in page
    harness.client.post("/settings/maintenance/clear-snapshots", follow_redirects=False)
    assert not legacy.exists()


def test_the_newest_failure_is_listed_first(harness: AppHarness) -> None:
    """Twenty are kept; the one that just happened is the one being diagnosed."""
    import os

    harness.activate()
    older = _write_snapshot(harness, name=f"{SNAPSHOT_PREFIX}10-00000000{SNAPSHOT_SUFFIX}")
    newer = _write_snapshot(harness, name=f"{SNAPSHOT_PREFIX}20-11111111{SNAPSHOT_SUFFIX}")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    page = harness.client.get("/settings").text

    assert page.index(newer.name) < page.index(older.name)
