"""Per-movie ignore: store behavior + dashboard toggle."""

from __future__ import annotations

from pathlib import Path

from app.core.audit import AuditLog
from app.services.ignore import IgnoredMovie, IgnoreSnapshot, IgnoreStore
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


def _store(tmp_path: Path) -> IgnoreStore:
    return IgnoreStore(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"))


def test_ignore_by_tmdb_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(tmdb_id=555, title="Neon Rain", normalized_title="neon rain")
    assert store.is_ignored(555, "different") is True   # the id is what identifies it
    # Changed by review Step 10: a DIFFERENT id with the same title is a different film
    # (a remake or re-release) and must stay addable. The title is only a fallback for
    # entries with no id to compare — see tests/unit/test_ignore_snapshot.py.
    assert store.is_ignored(999, "neon rain") is False
    assert store.is_ignored(None, "neon rain") is True  # nothing but the title to go on
    assert store.is_ignored(999, "other") is False


def test_ignore_dedupes_and_removes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(tmdb_id=555, title="Neon Rain", normalized_title="neon rain")
    store.add(tmdb_id=555, title="Neon Rain", normalized_title="neon rain")
    assert len(store.list_ignored()) == 1
    store.remove(tmdb_id=555, normalized_title="neon rain")
    assert store.list_ignored() == []
    assert store.is_ignored(555, "neon rain") is False


def test_dashboard_ignore_toggle(harness: AppHarness) -> None:
    harness.activate()
    ignore = harness.client.app.state.ignore
    response = harness.client.post(
        "/ignore",
        data={"tmdb_id": "555", "title": "Neon Rain", "normalized_title": "neon rain"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ignore.is_ignored(555, "neon rain") is True

    harness.client.post(
        "/unignore",
        data={"tmdb_id": "555", "normalized_title": "neon rain"},
        follow_redirects=False,
    )
    assert ignore.is_ignored(555, "neon rain") is False


def _ignore_two(harness: AppHarness) -> None:
    store = harness.client.app.state.ignore
    store.add(tmdb_id=1, title="Cats", normalized_title="cats")
    store.add(tmdb_id=2, title="Morbius", normalized_title="morbius")


def test_ignored_titles_are_collapsed_behind_a_count(harness: AppHarness) -> None:
    """A title you ignored is one you decided not to think about again — it should not
    take up the settings page until you ask for it."""
    harness.activate()
    _ignore_two(harness)

    page = harness.client.get("/settings").text

    assert "Show ignored titles (2)" in page  # the count is judgeable without opening
    section = page.split('class="section-title">Ignored Titles')[1]
    assert "<details>" in section
    assert "<details open" not in section  # closed on arrival


def test_ignored_titles_sit_last_on_the_settings_page(harness: AppHarness) -> None:
    # Below Backups, so the sections you actually change stay above the fold.
    harness.activate()
    page = harness.client.get("/settings").text

    # Match the headings, not prose — the first-run banner also mentions "External Apps".
    def heading(text: str) -> int:
        return page.index(f'class="section-title">{text}')

    order = [
        heading("User Management"),
        heading("External Apps"),
        heading("Weekly Check"),
        heading("Backups"),
        heading("Ignored Titles"),
    ]
    assert order == sorted(order)
    assert order[-1] == heading("Ignored Titles")  # last, not merely after Backups


def test_the_list_is_still_there_when_opened(harness: AppHarness) -> None:
    # Collapsed, not removed: the rows and their un-ignore forms are server-rendered.
    harness.activate()
    _ignore_two(harness)

    page = harness.client.get("/settings").text
    assert "Cats" in page and "Morbius" in page
    assert page.count('action="/unignore"') == 2


def test_un_ignoring_from_settings_still_works(harness: AppHarness) -> None:
    harness.activate()
    _ignore_two(harness)

    response = harness.client.post(
        "/unignore",
        data={"tmdb_id": "1", "normalized_title": "cats", "next": "settings"},
        follow_redirects=False,
    )

    assert "/settings?status=unignored" in response.headers["location"]
    assert "Show ignored titles (1)" in harness.client.get("/settings").text


def test_an_empty_list_shows_no_disclosure(harness: AppHarness) -> None:
    # Nothing to collapse — a closed <details> reading "(0)" would be a dead control.
    harness.activate()
    page = harness.client.get("/settings").text
    section = page.split('class="section-title">Ignored Titles')[1]
    assert "Show ignored titles" not in section
    assert "Nothing ignored." in section


# --- the weeks an ignored title charted in, each opening that report ---

CATS_TMDB = 1
REMAKE_TMDB = 99


def _report(week: str, movies: list[MovieResult], *, run_at: str = "", suffix: str = "a") -> Report:
    return Report(
        id=f"report-{week}-{suffix}", run_at=run_at or "2026-08-12T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
        totals=ReportTotals(movies=len(movies), matched=0), movies=movies,
    )


def _movie(rank: int, title: str, tmdb: int | None, normalized: str = "") -> MovieResult:
    return MovieResult(
        rank=rank, title=title, normalized_title=normalized or title.lower(),
        gross_amount=1_000_000, gross_display="$1.0M", weeks_in_release=1,
        status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=tmdb,
    )


def _chips(page: str) -> list[str]:
    section = page.split('class="section-title">Ignored Titles')[1]
    return [
        chunk.partition(">")[2].split("</a>")[0].strip()
        for chunk in section.split('class="week-chip" href')[1:]
    ]


def test_an_ignored_title_lists_the_weeks_it_charted(harness: AppHarness) -> None:
    """Same as the search results: knowing a title is ignored is only half the answer —
    the other half is which week it was on, without opening every report."""
    harness.activate()
    _ignore_two(harness)
    for week in ("2026W29", "2026W31"):
        harness.client.app.state.reports.save(
            _report(week, [_movie(3, "Cats", CATS_TMDB)], suffix=week)
        )

    page = harness.client.get("/settings").text

    assert _chips(page) == ["W31 · #3", "W29 · #3"]  # newest week first


def test_each_ignored_week_links_to_that_report(harness: AppHarness) -> None:
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(
        _report("2026W31", [_movie(3, "Cats", CATS_TMDB)], suffix="x")
    )

    page = harness.client.get("/settings").text

    assert 'href="/reports/report-2026W31-x#movie-3"' in page


def test_an_ignored_title_never_borrows_a_remake_weeks(harness: AppHarness) -> None:
    """The identity rule the ignore check itself uses: two films that both carry TMDB ids
    are the same film only if the ids agree. A 2026 remake normalizes to the same title
    as the original and is a different film."""
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(
        _report("2026W31", [_movie(3, "Cats", REMAKE_TMDB)], suffix="remake")
    )

    page = harness.client.get("/settings").text

    assert _chips(page) == []  # the ignored Cats is tmdb 1, not 99


def test_an_entry_without_a_tmdb_id_still_finds_its_weeks(harness: AppHarness) -> None:
    # A chart title Radarr could not identify is ignored by title, and matched by it too.
    harness.activate()
    harness.client.app.state.ignore.add(
        tmdb_id=None, title="Obscure Doc", normalized_title="obscure doc"
    )
    harness.client.app.state.reports.save(
        _report("2026W30", [_movie(9, "Obscure Doc", None)], suffix="u")
    )

    page = harness.client.get("/settings").text

    assert _chips(page) == ["W30 · #9"]


def test_a_rerun_week_is_listed_once(harness: AppHarness) -> None:
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(
        _report("2026W31", [_movie(3, "Cats", CATS_TMDB)],
                run_at="2026-08-12T10:00:00+00:00", suffix="first")
    )
    harness.client.app.state.reports.save(
        _report("2026W31", [_movie(4, "Cats", CATS_TMDB)],
                run_at="2026-08-20T10:00:00+00:00", suffix="rerun")
    )

    page = harness.client.get("/settings").text

    assert _chips(page) == ["W31 · #4"]  # the freshest run of the week, once


def test_a_failed_run_contributes_no_week(harness: AppHarness) -> None:
    """A run that failed has no chart to send anyone to.

    The movies here are deliberately constructed: every failure path writes a report
    without them today, so a movie-less failed report would pass whether or not the
    status is checked at all. This pins the rule rather than today's happenstance.
    """
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(Report(
        id="report-2026W31-failed", run_at="2026-08-12T10:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED, week="2026W31",
        totals=ReportTotals(movies=1, matched=0), error="layout changed",
        movies=[_movie(3, "Cats", CATS_TMDB)],
    ))

    page = harness.client.get("/settings").text

    assert _chips(page) == []


def test_weeks_are_ordered_by_week_not_by_when_the_report_ran(
    harness: AppHarness,
) -> None:
    """Re-running an old week today makes its report the newest written, and its chip
    must still sit at the end of the run — the ordering the search chips already use."""
    harness.activate()
    _ignore_two(harness)
    for index, week in enumerate(("2026W29", "2026W30", "2026W31")):
        harness.client.app.state.reports.save(
            _report(week, [_movie(index + 1, "Cats", CATS_TMDB)],
                    run_at=f"2026-08-1{index}T10:00:00+00:00", suffix=week)
        )
    harness.client.app.state.reports.save(
        _report("2026W29", [_movie(9, "Cats", CATS_TMDB)],
                run_at="2026-08-31T23:59:59+00:00", suffix="rerun")
    )

    page = harness.client.get("/settings").text

    assert _chips(page) == ["W31 · #3", "W30 · #2", "W29 · #9"]


def test_an_ignored_title_with_no_surviving_report_still_lists(harness: AppHarness) -> None:
    """Reports get deleted and pruned; the ignore entry outlives them. The row must still
    render, with nothing to click rather than a broken link."""
    harness.activate()
    _ignore_two(harness)

    page = harness.client.get("/settings").text

    assert "Show ignored titles (2)" in page
    assert _chips(page) == []


def test_the_per_entry_rule_agrees_with_the_ignore_check() -> None:
    """`IgnoredMovie.matches` decides which weeks an entry owns; `IgnoreSnapshot`
    decides whether a chart title is hidden. Two statements of one rule, so they are
    checked against each other rather than trusted to stay in step: a title whose weeks
    are listed must be a title that is actually ignored, and the reverse.
    """
    entries = [
        IgnoredMovie(tmdb_id=1, title="Cats", normalized_title="cats"),
        IgnoredMovie(tmdb_id=None, title="Obscure Doc", normalized_title="obscure doc"),
    ]
    snapshot = IgnoreSnapshot(
        tmdb_ids=frozenset({1}),
        titles=frozenset({"cats", "obscure doc"}),
        unidentified_titles=frozenset({"obscure doc"}),
    )
    candidates = [
        (1, "cats"),             # the ignored film itself
        (99, "cats"),            # a remake: same title, different film
        (None, "cats"),          # unidentified, title is all there is
        (7, "obscure doc"),      # identified now, ignored when it was not
        (None, "obscure doc"),
        (None, "something else"),
        (42, "something else"),
    ]

    for tmdb_id, normalized_title in candidates:
        owned_by_an_entry = any(entry.matches(tmdb_id, normalized_title) for entry in entries)
        assert owned_by_an_entry == snapshot.is_ignored(tmdb_id, normalized_title), (
            f"disagreement on {(tmdb_id, normalized_title)}"
        )


def test_an_unresolved_current_week_offers_no_chip(harness: AppHarness) -> None:
    """A run whose week could not be resolved is stored as "current" — not a week anyone
    can be sent to, and the chip's label is built by slicing the week id, so it would
    read "Wnt" rather than fail loudly."""
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(
        _report("current", [_movie(3, "Cats", CATS_TMDB)], suffix="now")
    )

    page = harness.client.get("/settings").text

    assert _chips(page) == []
    assert "Wnt" not in page


def test_an_ignored_week_link_lands_on_the_title_itself(harness: AppHarness) -> None:
    """Same jump the search chips make: an ignored title you are reconsidering should
    arrive at the film, not at the top of that week's chart."""
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(_report("2026W31", [
        _movie(1, "Something Else", 77),
        _movie(6, "Cats", CATS_TMDB),
    ], suffix="x"))

    page = harness.client.get("/settings").text
    href = page.split('class="week-chip" href=')[1].split('"')[1]

    assert href == "/reports/report-2026W31-x#movie-6"  # Cats' own rank, not the first row

    path, _, fragment = href.partition("#")
    report = harness.client.get(path).text
    card = report.split(f'id="{fragment}"')[1]
    assert "Cats" in card.split("</article>")[0] or "Cats" in card[:600]


def test_ignored_chips_name_the_year_across_two(harness: AppHarness) -> None:
    """Same rule as the search chips — the partial is shared, so the labels must be too."""
    harness.activate()
    _ignore_two(harness)
    harness.client.app.state.reports.save(
        _report("2025W02", [_movie(3, "Cats", CATS_TMDB)], suffix="a25"))
    harness.client.app.state.reports.save(
        _report("2026W02", [_movie(6, "Cats", CATS_TMDB)], suffix="a26"))

    assert _chips(harness.client.get("/settings").text) == ["W02 ’26 · #6", "W02 ’25 · #3"]


def test_ignored_chips_stay_compact_within_one_year(harness: AppHarness) -> None:
    harness.activate()
    _ignore_two(harness)
    for week in ("2026W29", "2026W31"):
        harness.client.app.state.reports.save(
            _report(week, [_movie(3, "Cats", CATS_TMDB)], suffix=week))

    chips = _chips(harness.client.get("/settings").text)

    assert chips == ["W31 · #3", "W29 · #3"]
