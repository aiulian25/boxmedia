"""The month's highest-grossing titles, above the run controls on Weekly Reports."""

from __future__ import annotations

from app.services.reports import (
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.web.reports import MONTH_TOP_COUNT, _month_top_grossers
from tests.conftest import AppHarness

MILLION = 1_000_000


def _movie(
    rank: int, title: str, gross: int, tmdb: int, *, total: int | None = None
) -> MovieResult:
    return MovieResult(
        rank=rank, title=title, normalized_title=title.lower(),
        gross_amount=gross, gross_display=f"${gross / MILLION:.1f}M", weeks_in_release=1,
        total_gross=total,
        status=MovieStatus.MISSING, action=MovieAction.NONE, tmdb_id=tmdb,
    )


def _report(week: str, movies: list[MovieResult], *, run_at: str = "", suffix: str = "a") -> Report:
    return Report(
        id=f"report-{week}-{suffix}", run_at=run_at or "2026-08-12T10:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=RunStatus.OK, week=week,
        totals=ReportTotals(movies=len(movies), matched=0), movies=movies,
    )


def _save(harness: AppHarness, *reports: Report) -> None:
    for report in reports:
        harness.client.app.state.reports.save(report)


# 2026W27 starts 29/6 (June); W28-W31 start in July.
JULY_WEEKS = ("2026W28", "2026W29")


def test_titles_are_ranked_by_summed_monthly_gross(harness: AppHarness) -> None:
    """Mojo's "Gross" column is the WEEKLY figure — "Total Gross" is read separately and
    never summed — so a title's weeks add up to what it took that month."""
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "Slow Burner", 10 * MILLION, 1),
                            _movie(2, "One Big Weekend", 40 * MILLION, 2)]),
        _report("2026W29", [_movie(1, "Slow Burner", 35 * MILLION, 1)]),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert [entry["title"] for entry in top["entries"]] == ["Slow Burner", "One Big Weekend"]
    assert top["entries"][0]["gross_amount"] == 45 * MILLION  # 10 + 35, not just the best week
    assert top["entries"][0]["weeks"] == 2


def test_only_the_most_recent_month_with_data_is_shown(harness: AppHarness) -> None:
    """A strict "current month" would be empty whenever no week of it has been fetched —
    which is the normal state, since Mojo lags and weeks are fetched deliberately."""
    harness.activate()
    _save(
        harness,
        _report("2026W15", [_movie(1, "April Film", 99 * MILLION, 1)]),   # April
        _report("2026W28", [_movie(1, "July Film", 5 * MILLION, 2)]),     # July
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["label"] == "July 2026"
    assert [entry["title"] for entry in top["entries"]] == ["July Film"]  # April excluded


def test_the_heading_names_the_month(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [_movie(1, "July Film", 5 * MILLION, 1)]))

    page = harness.client.get("/reports").text

    assert "July 2026 — highest gross" in page
    assert "This month" not in page  # a claim that can be false is not made


def test_a_rerun_week_does_not_count_its_gross_twice(harness: AppHarness) -> None:
    """Re-running a week writes a second report for it. Counting both would inflate the
    month — the same deduplication histories() applies."""
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "July Film", 20 * MILLION, 1)],
                run_at="2026-08-12T10:00:00+00:00", suffix="first"),
        _report("2026W28", [_movie(1, "July Film", 20 * MILLION, 1)],
                run_at="2026-08-13T10:00:00+00:00", suffix="rerun"),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["entries"][0]["gross_amount"] == 20 * MILLION  # not 40
    assert top["weeks_tracked"] == 1


def test_only_five_titles_are_named(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [
        _movie(rank, f"Film {rank:02d}", (20 - rank) * MILLION, 100 + rank)
        for rank in range(1, 12)
    ]))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert len(top["entries"]) == MONTH_TOP_COUNT
    assert top["titles"] == 11  # the count still reports the whole month
    assert [entry["rank"] for entry in top["entries"]] == [1, 2, 3, 4, 5]


def test_a_failed_run_contributes_nothing(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [_movie(1, "July Film", 5 * MILLION, 1)]))
    harness.client.app.state.reports.save(Report(
        id="report-2026W29-failed", run_at="2026-08-14T10:00:00+00:00",
        trigger=RunTrigger.SCHEDULED, status=RunStatus.SCRAPE_FAILED, week="2026W29",
        totals=ReportTotals(movies=0, matched=0), error="layout changed",
    ))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["weeks_tracked"] == 1


def test_no_reports_means_no_section(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/reports").text

    assert _month_top_grossers([]) is None
    assert "highest gross" not in page  # the section is absent, not an empty box


def test_an_unresolved_current_week_is_skipped(harness: AppHarness) -> None:
    # "current" is not a real week and has no calendar month to belong to.
    harness.activate()
    _save(harness, _report("current", [_movie(1, "Unresolved", 5 * MILLION, 1)]))

    assert _month_top_grossers(harness.client.app.state.reports.list_reports()) is None


def test_the_strip_renders_posters_ranks_and_money(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [
        _movie(1, "Top Film", 40 * MILLION, 555),
        _movie(2, "Second Film", 10 * MILLION, 556),
    ]))

    page = harness.client.get("/reports").text
    strip = page.split('class="gross-strip"')[1].split("</section>")[0]

    assert "Top Film" in strip and "Second Film" in strip
    assert "$40.0M" in strip and "$10.0M" in strip
    assert 'class="rank-chip">1<' in strip
    assert 'data-movie="555"' in strip  # opens in the same modal as the weekly cards


def test_the_strip_sits_above_the_run_controls(harness: AppHarness) -> None:
    """Asked for at the top of the page — and the primary action must stay reachable
    right below it."""
    harness.activate()
    _save(harness, _report("2026W28", [_movie(1, "Top Film", 40 * MILLION, 555)]))

    page = harness.client.get("/reports").text

    assert page.index('class="top-gross"') < page.index('class="run-actions"')


# --- always the current month, falling back to last month until it has a week ---


def _full_july(harness: AppHarness) -> None:
    _save(harness, *[
        _report(week, [_movie(1, "July Hit", 100 * MILLION, 1)], suffix=week)
        for week in ("2026W28", "2026W29", "2026W30", "2026W31")
    ])


def test_last_month_stands_in_until_the_new_one_has_a_week(harness: AppHarness) -> None:
    """A new month starts with nothing fetched for it, and the section must not go
    blank while waiting for the first week to land."""
    harness.activate()
    _full_july(harness)

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["label"] == "July 2026"
    assert top["weeks_tracked"] == 4


def test_the_new_month_takes_over_on_its_very_first_week(harness: AppHarness) -> None:
    """One week is enough: "this month" should mean this month the moment there is any
    of it, even though the list is thin at first."""
    harness.activate()
    _full_july(harness)
    _save(harness, _report("2026W32", [_movie(1, "August Newcomer", 3 * MILLION, 2)],
                           suffix="aug1"))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["label"] == "August 2026"
    assert top["weeks_tracked"] == 1
    assert [entry["title"] for entry in top["entries"]] == ["August Newcomer"]


def test_each_further_week_updates_the_running_month(harness: AppHarness) -> None:
    """"Update them on every new report": a second week of the month adds to the totals
    and can reorder the five, rather than replacing them."""
    harness.activate()
    _full_july(harness)
    _save(harness, _report("2026W32", [
        _movie(1, "Fast Starter", 30 * MILLION, 2),
        _movie(2, "Slow Climber", 5 * MILLION, 3),
    ], suffix="aug1"))

    first_week = _month_top_grossers(harness.client.app.state.reports.list_reports())
    assert [entry["title"] for entry in first_week["entries"]] == ["Fast Starter", "Slow Climber"]

    _save(harness, _report("2026W33", [
        _movie(1, "Slow Climber", 40 * MILLION, 3),
        _movie(2, "Fast Starter", 2 * MILLION, 2),
    ], suffix="aug2"))

    second_week = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert second_week["weeks_tracked"] == 2
    # 45M beats 32M — the running total reorders them, it does not start over.
    assert [entry["title"] for entry in second_week["entries"]] == ["Slow Climber", "Fast Starter"]
    assert second_week["entries"][0]["gross_amount"] == 45 * MILLION


def test_a_fresh_install_still_shows_its_only_week(harness: AppHarness) -> None:
    """The threshold must not mean "show nothing" — one week is better than an empty
    section on a new install."""
    harness.activate()
    _save(harness, _report("2026W33", [_movie(1, "Only Week", 9 * MILLION, 1)], suffix="x"))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["label"] == "August 2026"
    assert top["weeks_tracked"] == 1


# --- the running total, shown beside the monthly figure (option C) ---


def test_the_running_total_is_shown_beside_the_monthly_take(harness: AppHarness) -> None:
    """A film in its ninth week can take $6M this month against $473M overall. Ranking on
    the month is right, but the monthly number alone reads as a mistake without it."""
    harness.activate()
    _save(harness, _report("2026W28", [
        _movie(1, "Long Runner", 6 * MILLION, 1, total=473 * MILLION),
    ]))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["entries"][0]["gross_amount"] == 6 * MILLION       # ranked on the month
    assert top["entries"][0]["gross_display"] == "$6.0M"
    assert top["entries"][0]["total_gross_display"] == "$473.0M"  # shown, never ranked on


def test_the_total_is_never_summed_across_weeks(harness: AppHarness) -> None:
    """The whole point of the weekly column: adding two cumulative figures counts the
    same money twice and would put a modest film above a genuine hit."""
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "Long Runner", 5 * MILLION, 1, total=400 * MILLION)]),
        _report("2026W29", [_movie(1, "Long Runner", 4 * MILLION, 1, total=404 * MILLION)],
                suffix="b"),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["entries"][0]["gross_amount"] == 9 * MILLION        # 5 + 4, the month
    assert top["entries"][0]["total_gross_display"] == "$404.0M"   # latest, not 804M


def test_the_ranking_ignores_the_total(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [
        _movie(1, "Veteran", 6 * MILLION, 1, total=473 * MILLION),
        _movie(2, "Newcomer", 40 * MILLION, 2, total=40 * MILLION),
    ]))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    # Newcomer took more THIS MONTH, so it leads — the heading says "highest gross" for
    # the month and the order has to match it.
    assert [entry["title"] for entry in top["entries"]] == ["Newcomer", "Veteran"]


def test_a_rerun_of_an_older_week_does_not_roll_the_total_back(harness: AppHarness) -> None:
    """Reports come back newest-RUN first, which is not newest-WEEK first once a week is
    re-run. Taking the largest keeps the figure current either way."""
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "Long Runner", 5 * MILLION, 1, total=400 * MILLION)],
                run_at="2026-08-11T10:00:00+00:00", suffix="w28"),
        _report("2026W29", [_movie(1, "Long Runner", 4 * MILLION, 1, total=404 * MILLION)],
                run_at="2026-08-12T10:00:00+00:00", suffix="w29"),
        # Re-run of the EARLIER week, done last: it sorts to the front of the list.
        _report("2026W28", [_movie(1, "Long Runner", 5 * MILLION, 1, total=400 * MILLION)],
                run_at="2026-08-20T10:00:00+00:00", suffix="w28-rerun"),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["entries"][0]["total_gross_display"] == "$404.0M"  # not rolled back to 400M


def test_a_report_stored_before_the_column_was_read_shows_no_total(
    harness: AppHarness,
) -> None:
    """Every existing report predates this, so the card must be complete without it —
    a total appears as those weeks are re-run."""
    harness.activate()
    _save(harness, _report("2026W28", [_movie(1, "Old Record", 5 * MILLION, 555)]))

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())
    strip = harness.client.get("/reports").text.split('class="gross-strip"')[1]

    assert top["entries"][0]["total_gross_display"] is None
    assert "Old Record" in strip and "$5.0M" in strip  # the card is otherwise unchanged
    assert "total" not in strip.split("</section>")[0]  # no dangling separator


def test_the_card_shows_both_figures(harness: AppHarness) -> None:
    harness.activate()
    _save(harness, _report("2026W28", [
        _movie(1, "Long Runner", 6 * MILLION, 555, total=473 * MILLION),
    ]))

    strip = harness.client.get("/reports").text.split('class="gross-strip"')[1]

    assert "$6.0M" in strip     # the overlay: what it took this month
    assert "$473.0M total" in strip


# --- one currency per board, and it says what it left out (M2) ---


def _in_currency(report: Report, currency: str, region: str = "GB") -> Report:
    return report.model_copy(update={"currency": currency, "region": region})


def test_a_month_mixing_currencies_ranks_only_one_of_them(harness: AppHarness) -> None:
    """The number this exists for. Adding pounds to dollars would not mislabel the
    ranking — it would reorder it, putting a smaller take above a larger one."""
    harness.activate()
    # Distinct run_at values, so "the newest contributing report" is not left to the
    # order a directory listing happens to return.
    _save(
        harness,
        _report("2026W28", [_movie(1, "Dollar Film", 40 * MILLION, 1)],
                run_at="2026-08-20T10:00:00+00:00"),
        _report("2026W29", [_movie(1, "Dollar Film", 5 * MILLION, 1)],
                run_at="2026-08-19T10:00:00+00:00"),
        _in_currency(
            _report("2026W30", [_movie(1, "Pound Film", 90 * MILLION, 2)], suffix="b",
                    run_at="2026-08-01T10:00:00+00:00"),
            "£",
        ),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert [entry["title"] for entry in top["entries"]] == ["Dollar Film"]
    assert top["entries"][0]["gross_display"] == "$45.0M"
    assert top["weeks_tracked"] == 2       # the weeks it is actually built from
    assert top["excluded_weeks"] == 1
    assert top["titles"] == 1


def test_the_newest_report_decides_which_currency_the_board_speaks(
    harness: AppHarness,
) -> None:
    """The same month as above with the pound week written LAST — so the board speaks
    pounds and excludes the dollars instead. Which currency wins is not arbitrary: it is
    whichever the install most recently fetched in.
    """
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "Dollar Film", 40 * MILLION, 1)],
                run_at="2026-08-01T10:00:00+00:00"),
        _in_currency(
            _report("2026W29", [_movie(1, "Pound Film", 9 * MILLION, 2)], suffix="b",
                    run_at="2026-08-20T10:00:00+00:00"),
            "£",
        ),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert [entry["title"] for entry in top["entries"]] == ["Pound Film"]
    assert top["entries"][0]["gross_display"] == "£9.0M"
    assert top["excluded_weeks"] == 1


def test_an_all_pound_month_reads_in_pounds(harness: AppHarness) -> None:
    harness.activate()
    _save(
        harness,
        _in_currency(_report("2026W28", [_movie(1, "Pound Film", 4 * MILLION, 1,
                                                total=40 * MILLION)]), "£"),
        _in_currency(_report("2026W29", [_movie(1, "Pound Film", 2 * MILLION, 1,
                                                total=42 * MILLION)]), "£"),
    )

    top = _month_top_grossers(harness.client.app.state.reports.list_reports())

    assert top["entries"][0]["gross_display"] == "£6.0M"
    assert top["entries"][0]["total_gross_display"] == "£42.0M"
    assert top["excluded_weeks"] == 0


def test_a_single_currency_month_says_nothing_about_exclusions(
    harness: AppHarness,
) -> None:
    """The usual case must not gain a clause about a problem it does not have."""
    harness.activate()
    _save(harness, _report("2026W28", [_movie(1, "Dollar Film", 40 * MILLION, 1)]))

    page = harness.client.get("/reports").text

    assert "highest gross" in page
    assert "excluded" not in page


def test_the_meta_line_names_what_it_left_out(harness: AppHarness) -> None:
    """A leaderboard quietly built from half a month is worse than one that admits it."""
    harness.activate()
    _save(
        harness,
        _report("2026W28", [_movie(1, "Dollar Film", 40 * MILLION, 1)],
                run_at="2026-08-20T10:00:00+00:00"),
        _in_currency(
            _report("2026W29", [_movie(1, "Pound Film", 90 * MILLION, 2)], suffix="b",
                    run_at="2026-08-01T10:00:00+00:00"), "£"
        ),
    )

    page = harness.client.get("/reports").text

    assert "1 week tracked" in page
    assert "1 week in another region excluded" in page
