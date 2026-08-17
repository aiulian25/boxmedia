"""ReportsStore: prune retention, and the per-title week-by-week history."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.reports import (
    MAX_BACKFILL_WEEKS,
    MAX_REPORTS,
    MovieResult,
    MovieStatus,
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)


def _report(index: int) -> Report:
    return Report(
        id=f"report-{index:04d}",
        run_at=f"2026-08-14T00:00:00.{index:04d}+00:00",  # sortable: higher index = newer
        trigger=RunTrigger.MANUAL,
        status=RunStatus.OK,
        totals=ReportTotals(movies=0, matched=0),
    )


def _movie(rank: int, title: str, gross: int) -> MovieResult:
    return MovieResult(
        rank=rank,
        title=title,
        normalized_title=title.lower(),
        gross_amount=gross,
        gross_display=f"${gross}",
        weeks_in_release=1,
        status=MovieStatus.MISSING,
        action="none",
    )


def test_prune_keeps_only_newest(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    total = MAX_REPORTS + 10
    for index in range(total):
        store.save(_report(index))
    assert len(store.list_reports()) == total  # nothing pruned until asked

    store.prune()
    remaining = store.list_reports()
    assert len(remaining) == MAX_REPORTS
    assert remaining[0].id == f"report-{total - 1:04d}"  # newest survives
    assert remaining[-1].id == f"report-{total - MAX_REPORTS:04d}"  # oldest kept boundary


def test_prune_noop_below_cap(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    for index in range(3):
        store.save(_report(index))
    store.prune()
    assert len(store.list_reports()) == 3


def _week_report(index: int, week: str, movies: list[MovieResult]) -> Report:
    return _report(index).model_copy(update={"week": week, "movies": movies})


def test_histories_are_chronological_and_dedupe_reruns(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_week_report(0, "2026W30", [_movie(1, "Dune", 80), _movie(2, "Wicked", 40)]))
    store.save(_week_report(1, "2026W31", [_movie(3, "Dune", 25)]))
    # A re-run of an already-reported week must not double-count it.
    store.save(_week_report(2, "2026W31", [_movie(4, "Dune", 20)]))

    histories = store.histories()
    # Oldest first; the re-run of W31 replaced it rather than adding a second entry.
    # The currency travels with each figure — a gross without it cannot be added to
    # anything safely, now that a history can legitimately mix them.
    assert histories["dune"] == [("2026W30", 1, 80, "$"), ("2026W31", 4, 20, "$")]
    assert histories["wicked"] == [("2026W30", 2, 40, "$")]


def test_histories_skip_failed_and_unresolved_runs(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_week_report(0, "2026W30", [_movie(1, "Dune", 80)]))
    failed = _week_report(1, "2026W31", [_movie(2, "Dune", 50)])
    store.save(failed.model_copy(update={"status": RunStatus.SCRAPE_FAILED}))
    store.save(_week_report(2, "current", [_movie(3, "Dune", 10)]))

    assert store.histories()["dune"] == [("2026W30", 1, 80, "$")]


# --- one unreadable file must not brick every page (review step 6) ---


def _write_report(store_dir: Path, report_id: str) -> None:
    ReportsStore(store_dir).save(
        Report(
            id=report_id, run_at="2026-08-12T10:00:00+00:00", trigger=RunTrigger.MANUAL,
            status=RunStatus.OK, week="2026W27", totals=ReportTotals(movies=0, matched=0),
        )
    )


def test_a_corrupt_file_is_skipped_not_fatal(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    _write_report(tmp_path, "report-20260812-100000-good")
    (tmp_path / "report-20260812-110000-bad.json").write_text("{not json", encoding="utf-8")

    reports = ReportsStore(tmp_path).list_reports()

    assert [report.id for report in reports] == ["report-20260812-100000-good"]
    assert "skipping unreadable report" in capsys.readouterr().out
    # The evidence stays on disk.
    assert (tmp_path / "report-20260812-110000-bad.json").exists()


def test_a_future_schema_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    """The reproduction from the review: one schema_version 2 file used to raise
    SchemaVersionError out of every page that lists reports."""
    _write_report(tmp_path, "report-20260812-100000-good")
    (tmp_path / "report-20990101-000000-beef.json").write_text(
        json.dumps({"schema_version": 2, "id": "report-20990101-000000-beef"}),
        encoding="utf-8",
    )

    reports = ReportsStore(tmp_path).list_reports()

    assert [report.id for report in reports] == ["report-20260812-100000-good"]


def test_a_valid_json_file_with_the_wrong_shape_is_skipped(tmp_path: Path) -> None:
    # pydantic's ValidationError, not a parse error — a different failure, same handling.
    _write_report(tmp_path, "report-20260812-100000-good")
    (tmp_path / "report-20260812-120000-shape.json").write_text(
        json.dumps({"schema_version": 1, "id": "x"}), encoding="utf-8"
    )

    assert len(ReportsStore(tmp_path).list_reports()) == 1


def test_a_file_pruned_mid_read_is_skipped_silently(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    """`prune` deletes files while the sync routes list them from the threadpool, so a
    file really can vanish between the glob and the read. That is not a corruption and
    must not be reported as one."""
    _write_report(tmp_path, "report-20260812-100000-good")
    vanishing = tmp_path / "report-20260812-130000-gone.json"
    vanishing.write_text(json.dumps({"schema_version": 1, "id": "gone"}), encoding="utf-8")

    real_read_text = Path.read_text

    def read_text_after_deleting(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == vanishing.name:
            self.unlink()
        return real_read_text(self, *args, **kwargs)

    Path.read_text = read_text_after_deleting
    try:
        reports = ReportsStore(tmp_path).list_reports()
    finally:
        Path.read_text = real_read_text

    assert [report.id for report in reports] == ["report-20260812-100000-good"]
    assert "skipping unreadable report" not in capsys.readouterr().out


def test_a_broken_file_is_reported_once_not_on_every_page_view(
    tmp_path: Path, capsys  # noqa: ANN001
) -> None:
    """list_reports runs on every page view; printing each time would rotate the real
    diagnostics out of `docker logs`."""
    (tmp_path / "report-20260812-110000-bad.json").write_text("{not json", encoding="utf-8")
    store = ReportsStore(tmp_path)

    for _ in range(5):
        store.list_reports()

    assert capsys.readouterr().out.count("skipping unreadable report") == 1


def test_prune_leaves_an_unreadable_file_alone(tmp_path: Path) -> None:
    """A skipped file is invisible to `prune`, so it is never deleted — the evidence
    survives, and it does not consume a retention slot either."""
    for index in range(3):
        _write_report(tmp_path, f"report-2026081{index}-100000-ok{index}")
    broken = tmp_path / "report-20260812-110000-bad.json"
    broken.write_text("{not json", encoding="utf-8")

    ReportsStore(tmp_path).prune(keep=1)

    assert broken.exists()
    assert len(ReportsStore(tmp_path).list_reports()) == 1


# --- latest_for_week: what a fresh run is compared against ---


def _stored_week(report_id: str, week: str, *, run_at: str, status: str) -> Report:
    return Report(
        id=report_id, run_at=run_at, trigger=RunTrigger.MANUAL, status=status, week=week,
        totals=ReportTotals(movies=0, matched=0),
    )


def test_latest_for_week_ignores_a_later_failed_attempt(tmp_path: Path) -> None:
    """A failed run has no chart in it. If it were returned as "what we have for this
    week", a fresh run would be compared against nothing and the comparison would decide
    on garbage — so only completed runs count.
    """
    store = ReportsStore(tmp_path)
    store.save(_stored_week("report-ok", "2026W02",
                            run_at="2026-01-09T10:00:00+00:00", status=RunStatus.OK))
    store.save(_stored_week("report-failed", "2026W02",
                            run_at="2026-01-10T10:00:00+00:00",
                            status=RunStatus.SCRAPE_FAILED))

    assert store.latest_for_week("2026W02").id == "report-ok"


def test_latest_for_week_takes_the_freshest_of_several(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_stored_week("report-old", "2026W02",
                            run_at="2026-01-09T10:00:00+00:00", status=RunStatus.OK))
    store.save(_stored_week("report-new", "2026W02",
                            run_at="2026-01-11T10:00:00+00:00", status=RunStatus.OK))

    assert store.latest_for_week("2026W02").id == "report-new"


def test_latest_for_week_is_none_when_the_week_is_unknown(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_stored_week("report-ok", "2026W02",
                            run_at="2026-01-09T10:00:00+00:00", status=RunStatus.OK))

    assert store.latest_for_week("2026W03") is None


# --- one completed report per week, however often it is fetched ---


def _ok_week(report_id: str, week: str, run_at: str) -> Report:
    return Report(
        id=report_id, run_at=run_at, trigger=RunTrigger.MANUAL, status=RunStatus.OK,
        week=week, totals=ReportTotals(movies=0, matched=0),
    )


def test_collapsing_keeps_only_the_freshest_report_for_a_week(tmp_path: Path) -> None:
    """The reported state: one week showing up as three cards because it was fetched by
    hand, then again, then by the scheduler."""
    store = ReportsStore(tmp_path)
    store.save(_ok_week("report-a", "2026W32", "2026-08-10T10:00:00+00:00"))
    store.save(_ok_week("report-b", "2026W32", "2026-08-12T10:00:00+00:00"))
    store.save(_ok_week("report-c", "2026W32", "2026-08-14T10:00:00+00:00"))
    store.save(_ok_week("report-other", "2026W31", "2026-08-05T10:00:00+00:00"))

    removed = store.collapse_duplicate_weeks()

    assert removed == 2
    assert {r.id for r in store.list_reports()} == {"report-c", "report-other"}


def test_collapsing_leaves_a_failed_attempt_alone(tmp_path: Path) -> None:
    """A failure records an attempt that did not happen, not a chart. Folding it into the
    week would either destroy good data or hide that a run failed."""
    store = ReportsStore(tmp_path)
    store.save(_ok_week("report-ok", "2026W32", "2026-08-10T10:00:00+00:00"))
    store.save(Report(
        id="report-failed", run_at="2026-08-14T10:00:00+00:00", trigger=RunTrigger.SCHEDULED,
        status=RunStatus.SCRAPE_FAILED, week="2026W32",
        totals=ReportTotals(movies=0, matched=0), error="layout changed",
    ))

    store.collapse_duplicate_weeks()

    assert {r.id for r in store.list_reports()} == {"report-ok", "report-failed"}


def test_replacing_a_week_removes_what_was_there(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_ok_week("report-old", "2026W32", "2026-08-10T10:00:00+00:00"))

    store.replace_week(_ok_week("report-new", "2026W32", "2026-08-14T10:00:00+00:00"))

    assert [r.id for r in store.list_reports()] == ["report-new"]


def test_collapsing_an_already_clean_history_changes_nothing(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    store.save(_ok_week("report-a", "2026W31", "2026-08-05T10:00:00+00:00"))
    store.save(_ok_week("report-b", "2026W32", "2026-08-12T10:00:00+00:00"))

    assert store.collapse_duplicate_weeks() == 0
    assert len(store.list_reports()) == 2


# --- the holes in stored history (F19) ---


def _gap_report(week: str, *, status: str = RunStatus.OK) -> Report:
    return Report(
        id=f"report-{week}-{status[:4]}",
        run_at="2026-08-14T00:00:00+00:00",
        trigger=RunTrigger.MANUAL, status=status, week=week,
        totals=ReportTotals(movies=0, matched=0),
    )


def _store_with(tmp_path: Path, weeks: list[Report]) -> ReportsStore:
    store = ReportsStore(tmp_path)
    for report in weeks:
        store.save(report)
    return store


def test_a_single_gap_is_found(tmp_path: Path) -> None:
    store = _store_with(tmp_path, [_gap_report("2026W30"), _gap_report("2026W32")])

    assert store.missing_weeks() == ["2026W31"]


def test_a_contiguous_history_has_no_gaps(tmp_path: Path) -> None:
    store = _store_with(
        tmp_path, [_gap_report(f"2026W{n}") for n in ("30", "31", "32")]
    )

    assert store.missing_weeks() == []


def test_gaps_are_only_ever_inside_the_range_held(tmp_path: Path) -> None:
    """Never a proposal to fetch backwards into weeks that predate the install, or forward
    into ones that have not happened."""
    store = _store_with(tmp_path, [_gap_report("2026W30"), _gap_report("2026W33")])

    assert store.missing_weeks() == ["2026W31", "2026W32"]


def test_one_week_alone_has_no_inside_to_have_holes_in(tmp_path: Path) -> None:
    assert _store_with(tmp_path, [_gap_report("2026W30")]).missing_weeks() == []
    assert _store_with(tmp_path, []).missing_weeks() == []


def test_a_week_that_was_tried_and_failed_is_not_missing(tmp_path: Path) -> None:
    """The trap this rule exists for. Box Office Mojo genuinely has no data for some
    weeks; the fetch fails and stores a failed report. Counting only COMPLETED weeks here
    would make that week a hole again on every visit, and re-fetch it on every backfill,
    forever — an endless polite retry is still an endless retry.
    """
    store = _store_with(tmp_path, [
        _gap_report("2026W30"),
        _gap_report("2026W31", status=RunStatus.SCRAPE_FAILED),
        _gap_report("2026W32"),
    ])

    assert store.missing_weeks() == []


def test_the_unresolved_current_label_is_not_a_week(tmp_path: Path) -> None:
    """"current" is not a week anyone can be sent to, and it must not become one end of
    the range the gaps are measured across."""
    store = _store_with(tmp_path, [
        _gap_report("2026W30"), _gap_report("2026W32"), _gap_report("current"),
    ])

    assert store.missing_weeks() == ["2026W31"]


def test_a_run_of_gaps_crossing_new_year_walks_properly(tmp_path: Path) -> None:
    """Week arithmetic, not string arithmetic: 2026W01 does not follow 2026W52."""
    store = _store_with(tmp_path, [_gap_report("2025W51"), _gap_report("2026W02")])

    assert store.missing_weeks() == ["2025W52", "2026W01"]


def test_the_offer_is_capped_and_takes_the_oldest_first(tmp_path: Path) -> None:
    """Scrape politeness, the same kind of ceiling as the chart depth. Filling from the
    far end makes the history contiguous from a fixed point rather than leaving a moving
    frontier; a second click takes the next twelve."""
    # One week at each end, thirty clear weeks between them.
    store = _store_with(tmp_path, [_gap_report("2026W01"), _gap_report("2026W32")])

    offered = store.missing_weeks()

    assert len(offered) == MAX_BACKFILL_WEEKS == 12
    assert offered == [f"2026W{n:02d}" for n in range(2, 14)]
    assert offered == sorted(offered)
