"""Step 11 test: parse the fixture, fail loud on layout change + snapshot."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.services.boxoffice import (
    BOM_WEEKLY_URL,
    DOMESTIC_REGION,
    MAX_CHART_SIZE,
    MAX_RELEASE_LOOKUPS_PER_RUN,
    MAX_SNAPSHOTS,
    MIN_CHART_SIZE,
    MIN_EXPECTED_ROWS,
    RELEASE_IMDB_RE,
    SNAPSHOT_PREFIX,
    SNAPSHOT_SUFFIX,
    TOP_N,
    ScrapeError,
    _clean_percent,
    _currency_prefix,
    _snapshot_failure,
    bom_week_id,
    clear_snapshots,
    fetch_release_imdb_id,
    fetch_weekly_chart,
    find_latest_week,
    format_gross,
    is_week_id,
    list_snapshots,
    next_week_id,
    parse_chart,
    previous_week_id,
    snapshot_path,
    spans_multiple_years,
    validated_region,
    week_chart_url,
    week_chip_label,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "bom_weekly.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parses_top_ten_from_fixture() -> None:
    entries = parse_chart(_fixture_html())
    assert len(entries) == 10  # 11 rows in fixture, capped at TOP_N
    top = entries[0]
    assert top.rank == 1
    assert top.title == "Dune: Part Two"
    # The weekly "Gross" column is used, not "Total Gross".
    assert top.gross_amount == 81_514_000
    assert top.weeks_in_release == 1


def test_the_supported_depth_range_is_one_to_thirty() -> None:
    """The one place these are written out rather than imported.

    Every other test derives its expectations from these constants, which makes them
    consistent at ANY value and therefore silent about which value is right — moving the
    bounds moves the code and the assertions together. This is the anchor: 1 because
    tracking only the chart-topper is a real way to use this, 30 because that is the
    deepest weekly chart Box Office Mojo publishes, and the ceiling is scrape politeness
    rather than a parser limit.
    """
    assert (MIN_CHART_SIZE, MAX_CHART_SIZE) == (1, 30)
    # The default sits inside the range and is unchanged by widening it.
    assert MIN_CHART_SIZE <= TOP_N <= MAX_CHART_SIZE


def test_the_page_completeness_check_is_independent_of_the_depth() -> None:
    """`MIN_EXPECTED_ROWS` asks whether the PAGE looks like a chart, not whether the slice
    is full — so it must not be softened by a shallow depth."""
    assert MIN_EXPECTED_ROWS == 5
    assert MIN_EXPECTED_ROWS > MIN_CHART_SIZE  # ...which is only meaningful if it can bite


def test_parse_chart_honours_the_requested_depth() -> None:
    assert len(parse_chart(_fixture_html(), top_n=MIN_CHART_SIZE)) == MIN_CHART_SIZE
    # Asking for more than the chart holds yields everything it has, not an error. The
    # fixture has 11 rows and the ceiling is 30, so this is that guarantee at the top end.
    assert len(parse_chart(_fixture_html(), top_n=MAX_CHART_SIZE)) == 11
    # A literal depth, not the floor: this is about ORDER, and it should keep asserting
    # five ranks whatever the floor happens to be.
    ranks = [entry.rank for entry in parse_chart(_fixture_html(), top_n=5)]
    assert ranks == [1, 2, 3, 4, 5]  # depth cuts the tail, never reorders


def test_the_shallowest_depth_records_only_the_chart_topper() -> None:
    """A depth of one is a real way to use this: only the week's #1. It takes the top of
    the chart, never an arbitrary row."""
    entries = parse_chart(_fixture_html(), top_n=1)

    assert len(entries) == 1
    assert entries[0].rank == 1
    assert entries[0].title == "Dune: Part Two"


def test_a_thin_page_still_fails_however_shallow_the_depth() -> None:
    """The completeness check asks about the PAGE, not the slice. A week Box Office Mojo
    has barely started reporting must not be quietly recorded as a one-row success just
    because the operator only asked for one row.
    """
    thin = _fixture_html().split("<tr>")
    # Header plus two data rows — a real in-progress week, well under MIN_EXPECTED_ROWS.
    truncated = "<tr>".join(thin[:3]) + "</table></body></html>"

    with pytest.raises(ScrapeError):
        parse_chart(truncated, top_n=MIN_CHART_SIZE)


@respx.mock
async def test_fetch_threads_depth_into_the_parse(tmp_path: Path) -> None:
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text=_fixture_html()))
    _, entries = await fetch_weekly_chart(snapshot_dir=tmp_path / "sf", top_n=7)
    assert len(entries) == 7


def test_gross_formatting() -> None:
    assert format_gross(81_514_000) == "$81.5M"


def test_bom_week_id() -> None:
    assert bom_week_id(date(2026, 1, 5)) == "2026W02"  # ISO week of Jan 5, 2026
    assert bom_week_id(date(2026, 8, 12)) == "2026W33"


def test_week_chart_url() -> None:
    base = "https://www.boxofficemojo.com/weekly"
    assert week_chart_url(base, None) == "https://www.boxofficemojo.com/weekly/"
    assert week_chart_url(base, "current") == "https://www.boxofficemojo.com/weekly/"
    assert week_chart_url(base, "2026W02") == "https://www.boxofficemojo.com/weekly/2026W02/"


def test_missing_table_raises() -> None:
    with pytest.raises(ScrapeError):
        parse_chart("<html><body><p>no table here</p></body></html>")


def test_renamed_headers_raise() -> None:
    # Simulate a BOM redesign that renames the Release column.
    broken = _fixture_html().replace(">Release<", ">Movie<")
    with pytest.raises(ScrapeError):
        parse_chart(broken)


def test_truncated_chart_raises() -> None:
    # Header + only two data rows -> below MIN_EXPECTED_ROWS. A parseable-but-sparse
    # week reports "not fully reported yet", not a layout change.
    html = _fixture_html()
    head, _, _ = html.partition('<td class="mojo-field-type-rank">3</td>')
    with pytest.raises(ScrapeError, match="not be fully reported"):
        parse_chart(head + "</table></body></html>")


@respx.mock
async def test_fetch_snapshots_raw_html_on_parse_failure(tmp_path: Path) -> None:
    broken_html = "<html><body><p>redesigned, no table</p></body></html>"
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text=broken_html))
    snapshot_dir = tmp_path / "scrape-failures"
    with pytest.raises(ScrapeError):
        await fetch_weekly_chart(snapshot_dir=snapshot_dir)
    snapshots = list(snapshot_dir.glob("scrape-failure-*.html"))
    assert len(snapshots) == 1
    assert "redesigned" in snapshots[0].read_text(encoding="utf-8")


@respx.mock
async def test_fetch_parses_live_fixture(tmp_path: Path) -> None:
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text=_fixture_html()))
    resolved, entries = await fetch_weekly_chart(snapshot_dir=tmp_path / "sf")
    assert resolved == "current"  # bare page (no week links) -> "current"
    assert len(entries) == 10
    assert entries[0].title == "Dune: Part Two"


def test_find_latest_week() -> None:
    index = (
        '<a href="/weekly/2026W29/">x</a><a href="/weekly/2026W31/">y</a>'
        '<a href="/weekly/2026W30/">z</a>'
    )
    assert find_latest_week(index) == "2026W31"
    assert find_latest_week("no week links here") is None


@respx.mock
async def test_current_resolves_to_latest_indexed_week(tmp_path: Path) -> None:
    # The bare /weekly/ is an index listing weeks; "current" must fetch the latest one.
    index_html = '<a href="/weekly/2026W30/">a</a><a href="/weekly/2026W31/">b</a>'
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text=index_html))
    week_route = respx.get(f"{BOM_WEEKLY_URL}2026W31/").mock(
        return_value=httpx.Response(200, text=_fixture_html())
    )
    resolved, entries = await fetch_weekly_chart(snapshot_dir=tmp_path / "sf")
    assert resolved == "2026W31"  # the week actually fetched
    assert len(entries) == 10
    assert week_route.called  # fetched the latest week's chart, not the bare index


def test_previous_week_id() -> None:
    assert previous_week_id("2026W32") == "2026W31"
    assert previous_week_id("2026W02") == "2026W01"
    assert previous_week_id("2026W01") == "2025W52"  # year boundary
    assert previous_week_id("not-a-week") is None


def _thin_chart_html() -> str:
    # The fixture truncated to header + a single data row (rank 1) — an in-progress
    # week with too few rows to clear MIN_EXPECTED_ROWS.
    head, _, _ = _fixture_html().partition('<td class="mojo-field-type-rank">2</td>')
    return head + "</table></body></html>"


@respx.mock
async def test_current_falls_back_over_in_progress_week(tmp_path: Path) -> None:
    # Newest listed week (2026W32) is the running week with 1 row; "current" must step
    # back to the last complete week (2026W31).
    index_html = '<a href="/weekly/2026W31/">a</a><a href="/weekly/2026W32/">b</a>'
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text=index_html))
    thin_route = respx.get(f"{BOM_WEEKLY_URL}2026W32/").mock(
        return_value=httpx.Response(200, text=_thin_chart_html())
    )
    full_route = respx.get(f"{BOM_WEEKLY_URL}2026W31/").mock(
        return_value=httpx.Response(200, text=_fixture_html())
    )
    resolved, entries = await fetch_weekly_chart(snapshot_dir=tmp_path / "sf")
    assert resolved == "2026W31"  # stepped back to the last complete week
    assert len(entries) == 10
    assert thin_route.called and full_route.called  # tried W32, fell back to W31


@respx.mock
async def test_specific_week_with_no_data_errors_without_fallback(tmp_path: Path) -> None:
    # A picked/re-run week with no chart must error honestly, NOT fall back to another week.
    thin = respx.get(f"{BOM_WEEKLY_URL}2026W28/").mock(
        return_value=httpx.Response(200, text=_thin_chart_html())
    )
    prev = respx.get(f"{BOM_WEEKLY_URL}2026W27/").mock(
        return_value=httpx.Response(200, text=_fixture_html())
    )
    with pytest.raises(ScrapeError, match="No box-office data available for week 2026W28"):
        await fetch_weekly_chart(snapshot_dir=tmp_path / "sf", week="2026W28")
    assert thin.called
    assert not prev.called  # no silent fallback to the previous week


@respx.mock
async def test_specific_future_week_empty_page_reports_no_data(tmp_path: Path) -> None:
    # A far-future week returns HTTP 200 with no chart table; report "no data", not the
    # misleading "layout changed".
    respx.get(f"{BOM_WEEKLY_URL}2027W50/").mock(
        return_value=httpx.Response(200, text="<html><body><h1>Week 2027W50</h1></body></html>")
    )
    with pytest.raises(ScrapeError, match="No box-office data available for week 2027W50"):
        await fetch_weekly_chart(snapshot_dir=tmp_path / "sf", week="2027W50")


def test_next_week_id() -> None:
    assert next_week_id("2026W31") == "2026W32"
    assert next_week_id("2026W01") == "2026W02"
    assert next_week_id("2025W52") == "2026W01"  # year boundary
    assert next_week_id("not-a-week") is None


def test_clear_snapshots_removes_only_scrape_failures(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "scrape-failures"
    snapshot_dir.mkdir()
    (snapshot_dir / f"{SNAPSHOT_PREFIX}1234{SNAPSHOT_SUFFIX}").write_text("a", encoding="utf-8")
    (snapshot_dir / f"{SNAPSHOT_PREFIX}5678{SNAPSHOT_SUFFIX}").write_text("b", encoding="utf-8")
    unrelated = snapshot_dir / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    assert clear_snapshots(snapshot_dir) == 2
    assert list(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*")) == []
    assert unrelated.exists()


def test_clear_snapshots_on_a_missing_directory_is_a_noop(tmp_path: Path) -> None:
    assert clear_snapshots(tmp_path / "never-written") == 0


@respx.mock
async def test_a_failed_scrape_writes_a_snapshot_this_module_can_clear(tmp_path: Path) -> None:
    # Guards the writer/cleaner filename agreement: a snapshot the scraper writes must be
    # one clear_snapshots() matches.
    respx.get(BOM_WEEKLY_URL).mock(return_value=httpx.Response(200, text="<p>no table</p>"))
    snapshot_dir = tmp_path / "scrape-failures"
    with pytest.raises(ScrapeError):
        await fetch_weekly_chart(snapshot_dir=snapshot_dir)
    assert clear_snapshots(snapshot_dir) == 1


@pytest.mark.parametrize(
    ("week", "expected"),
    [
        ("2026W31", True),
        ("2026W01", True),
        ("2026W53", True),   # 2026 really does have 53 ISO weeks
        ("2025W53", False),  # 2025 does not — shape alone would wrongly accept it
        ("2026W99", False),
        ("2026W00", False),
        ("2026W1", False),
        ("26W31", False),
        ("current", False),
        ("", False),
        ("../../../robots.txt", False),
        ("2026W31/../../x", False),
    ],
)
def test_is_week_id(week: str, expected: bool) -> None:
    assert is_week_id(week) is expected


def test_two_failures_of_equal_length_no_longer_overwrite_each_other(
    tmp_path: Path,
) -> None:
    """The bug: named by length alone, a second failing page of the same size destroyed
    the first — exactly the evidence the snapshot exists to preserve."""
    first = "<html>" + "A" * 100 + "</html>"
    second = "<html>" + "B" * 100 + "</html>"
    assert len(first) == len(second)

    _snapshot_failure(tmp_path, first)
    _snapshot_failure(tmp_path, second)

    written = sorted(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.html"))
    assert written == sorted([first, second])


def test_the_same_failure_recurring_does_not_pile_up_copies(tmp_path: Path) -> None:
    """Named by content, so a scraper broken for months reuses one file per failure mode
    instead of writing an identical copy every run."""
    html = "<html>redesigned, no table</html>"
    for _ in range(5):
        _snapshot_failure(tmp_path, html)

    snapshots = list(tmp_path.glob("*.html"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == html


def test_snapshots_are_capped(tmp_path: Path) -> None:
    """A page carrying a timestamp hashes differently every run; the cap is what stops
    /data/logs growing without bound now that overwriting no longer bounds it."""
    for index in range(MAX_SNAPSHOTS + 15):
        _snapshot_failure(tmp_path, f"<html>failure {index}</html>")

    assert len(list(tmp_path.glob("*.html"))) == MAX_SNAPSHOTS


def test_the_cap_keeps_the_newest(tmp_path: Path) -> None:
    for index in range(MAX_SNAPSHOTS + 5):
        _snapshot_failure(tmp_path, f"<html>failure {index}</html>")

    kept = {path.read_text(encoding="utf-8") for path in tmp_path.glob("*.html")}
    assert "<html>failure 24</html>" in kept  # the last one written
    assert "<html>failure 0</html>" not in kept  # the oldest, pruned


def test_clear_snapshots_still_matches_the_new_names(tmp_path: Path) -> None:
    # Writer and cleaner must agree on the filename shape or the maintenance button
    # silently stops working.
    _snapshot_failure(tmp_path, "<html>a</html>")
    _snapshot_failure(tmp_path, "<html>bb</html>")
    unrelated = tmp_path / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    assert clear_snapshots(tmp_path) == 2
    assert list(tmp_path.glob("*.html")) == []
    assert unrelated.exists()


def test_an_empty_page_writes_nothing(tmp_path: Path) -> None:
    _snapshot_failure(tmp_path, "")
    assert list(tmp_path.glob("*.html")) == []


# --- the running total, read beside the weekly take (option C) ---

_TOTAL_GROSS_HEADER = (
    '<tr><th>Rank</th><th>Release</th><th>Gross</th>'
    '<th>Total Gross</th><th>Weeks</th></tr>'
)


def _chart(header: str, rows: str) -> str:
    return f"<html><body><table>{header}{rows}</table></body></html>"


def _rows(count: int, *, cells: str) -> str:
    return "".join(cells.format(rank=rank) for rank in range(1, count + 1))


def test_total_gross_is_read_from_its_own_column() -> None:
    """Mojo prints both: "Gross" is that week's take, "Total Gross" is the film's running
    box office. Confusing them was the whole reason a $715M film read as $205M."""
    html = _chart(_TOTAL_GROSS_HEADER, _rows(
        6, cells='<tr><td>{rank}</td><td>Film {rank}</td><td>$205,743,142</td>'
                 '<td>$715,831,670</td><td>2</td></tr>'))

    entries = parse_chart(html)

    assert entries[0].gross_amount == 205_743_142
    assert entries[0].total_gross == 715_831_670


def test_the_weekly_column_is_still_the_one_ranked_on() -> None:
    # "Gross" must not accidentally bind to "Total Gross" because it contains the word.
    html = _chart(
        '<tr><th>Rank</th><th>Release</th><th>Total Gross</th>'
        '<th>Gross</th><th>Weeks</th></tr>',
        _rows(6, cells='<tr><td>{rank}</td><td>Film {rank}</td><td>$900,000,000</td>'
                       '<td>$1,000,000</td><td>2</td></tr>'))

    entries = parse_chart(html)

    assert entries[0].gross_amount == 1_000_000     # the weekly column, whatever its order
    assert entries[0].total_gross == 900_000_000


def test_a_chart_without_the_total_column_still_parses() -> None:
    """The column is optional. Treating it as required would turn a layout change into
    no chart at all, which is exactly what the scraper is written to avoid."""
    html = _chart(
        '<tr><th>Rank</th><th>Release</th><th>Gross</th><th>Weeks</th></tr>',
        _rows(6, cells='<tr><td>{rank}</td><td>Film {rank}</td><td>$5,000,000</td>'
                       '<td>3</td></tr>'))

    entries = parse_chart(html)

    assert len(entries) == 6
    assert entries[0].total_gross is None


def test_a_row_missing_only_the_total_cell_is_still_kept() -> None:
    """The row-length guard counts the required columns only; counting the optional one
    would drop rows this parser accepts today."""
    short_row = ('<tr><td>1</td><td>Film 1</td><td>$5,000,000</td>'
                 '<td>3</td></tr>')  # no Total Gross cell, and Weeks lands before it
    html = _chart(
        '<tr><th>Rank</th><th>Release</th><th>Gross</th><th>Weeks</th>'
        '<th>Total Gross</th></tr>',
        short_row + _rows(5, cells='<tr><td>{rank}</td><td>Film {rank}</td>'
                                   '<td>$4,000,000</td><td>2</td><td>$40,000,000</td></tr>'))

    entries = parse_chart(html)

    assert len(entries) == 6
    assert entries[0].total_gross is None        # absent for the short row
    assert entries[1].total_gross == 40_000_000  # present for the rest


def test_an_unparseable_total_is_none_not_zero() -> None:
    # "-" appears for a film with no reported total; zero would be a claim.
    html = _chart(_TOTAL_GROSS_HEADER, _rows(
        6, cells='<tr><td>{rank}</td><td>Film {rank}</td><td>$5,000,000</td>'
                 '<td>-</td><td>1</td></tr>'))

    assert parse_chart(html)[0].total_gross is None


def test_a_running_total_past_a_billion_reads_in_billions() -> None:
    """Weekly takes never get near a billion, but the cumulative totals now shown beside
    them pass it routinely — and "$1205.0M" is wider and harder to read than "$1.21B"."""
    assert format_gross(1_205_000_000) == "$1.21B"
    assert format_gross(2_923_710_000) == "$2.92B"


def test_the_billion_boundary_is_tested_after_rounding() -> None:
    # 999,999,999 rounds to 1000.0 in millions: without this it prints "$1000.0M", the
    # one string wider than anything the card layout was measured against.
    assert format_gross(999_949_999) == "$999.9M"
    assert format_gross(999_999_999) == "$1.00B"
    assert format_gross(1_000_000_000) == "$1.00B"


# --- how a week reads on a chip or a trend line ---


def test_a_week_label_carries_the_year_only_when_it_is_needed() -> None:
    """Redundant in the usual case, and these sit in narrow places — a poster card and a
    table cell. Once two Januaries are stored, "W02" alone names two different weeks."""
    assert week_chip_label("2026W02", with_year=False) == "W02"
    assert week_chip_label("2026W02", with_year=True) == "W02 ’26"
    assert week_chip_label("2025W52", with_year=True) == "W52 ’25"


def test_a_label_never_slices_something_that_is_not_a_week() -> None:
    """Every caller builds these from stored report weeks, and a label is not the place to
    discover that changed — "current"[5:] would read as "nt"."""
    for value in ("current", "", "2026", "2026W99", "not-a-week"):
        assert week_chip_label(value, with_year=True) == value


def test_a_run_spanning_new_year_needs_the_year() -> None:
    assert spans_multiple_years(["2025W52", "2026W01"]) is True
    assert spans_multiple_years(["2026W02", "2026W31"]) is False
    assert spans_multiple_years(["2026W02"]) is False
    assert spans_multiple_years([]) is False


# --- who may read a snapshot, and by what name (F15) ---


def _snapshots(tmp_path: Path) -> Path:
    directory = tmp_path / "scrape-failures"
    directory.mkdir()
    (directory / f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}").write_text("evidence")
    (tmp_path / "audit.jsonl").write_text("secret")
    return directory


def test_only_a_name_this_module_writes_resolves_to_a_path(tmp_path: Path) -> None:
    """Tested here rather than only through the route, because the HTTP layer normalises
    `../` away before a handler ever sees it — every crafted URL 404s whether or not this
    guard exists, so a route test proves nothing about it. This is the function's own
    contract, and the one that holds if a future caller hands it a raw name.

    Each crafted name is WRITTEN to disk first. Without that the existence check answers
    for the allow-list, and a regex that had lost its anchors would still look correct:
    the name would match and the file simply would not be there.
    """
    directory = _snapshots(tmp_path)
    good = f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}"

    assert snapshot_path(directory, good) == directory / good

    crafted_names = (
        f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}.evil",   # anchored at the end
        f"evil{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}",     # ...and at the start
        f"{SNAPSHOT_PREFIX}1024-A1B2C3D4{SNAPSHOT_SUFFIX}",         # digest is lowercase
        f"{SNAPSHOT_PREFIX}1024-a1b2c3d{SNAPSHOT_SUFFIX}",          # ...and 8 characters
        f"{SNAPSHOT_PREFIX}1024-a1b2c3d4e{SNAPSHOT_SUFFIX}",
        f"{SNAPSHOT_PREFIX}-a1b2c3d4{SNAPSHOT_SUFFIX}",             # length is required
        f"{SNAPSHOT_PREFIX}abc-a1b2c3d4{SNAPSHOT_SUFFIX}",          # ...and is digits
    )
    for name in crafted_names:
        (directory / name).write_text("hostile", encoding="utf-8")

    traversals = (
        "../audit.jsonl",
        "../../etc/passwd",
        f"../{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}",
        f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}/../../audit.jsonl",
        "",
    )
    for crafted in crafted_names + traversals:
        assert snapshot_path(directory, crafted) is None, crafted


def test_a_name_that_matches_but_is_not_there_is_not_a_path(tmp_path: Path) -> None:
    """Cleared in another tab between the page render and the click."""
    directory = _snapshots(tmp_path)

    assert snapshot_path(directory, f"{SNAPSHOT_PREFIX}99-ffffffff{SNAPSHOT_SUFFIX}") is None


def test_the_listing_and_the_allow_list_agree(tmp_path: Path) -> None:
    """A listed row that 404s on click would be worse than no row, so the list is filtered
    through the same rule that serves it — not just the glob that clears it."""
    directory = _snapshots(tmp_path)
    (directory / f"{SNAPSHOT_PREFIX}42{SNAPSHOT_SUFFIX}").write_text("pre-digest")
    (directory / "notes.txt").write_text("not ours")

    listed = [name for name, _, _ in list_snapshots(directory)]

    assert listed == [f"{SNAPSHOT_PREFIX}1024-a1b2c3d4{SNAPSHOT_SUFFIX}"]
    for name in listed:
        assert snapshot_path(directory, name) is not None


def test_listing_a_directory_that_does_not_exist_is_empty_not_an_error(
    tmp_path: Path,
) -> None:
    assert list_snapshots(tmp_path / "nothing-here") == []


# --- the columns the parser used to download and throw away (M3) ---


def test_the_fixture_yields_its_real_screen_counts_and_moves() -> None:
    """Every run has been fetching these and discarding them at parse time."""
    entries = parse_chart(_fixture_html(), top_n=3)

    assert [(e.theaters, e.gross_change_pct) for e in entries] == [
        (4071, None),   # rank 1 is in its first week — Mojo prints "-", not 0%
        (3850, -38),
        (3600, -25),
    ]


def test_no_previous_week_is_not_a_flat_week() -> None:
    """"-" means there was nothing to compare against. Reading it as 0% would put a
    "▲ 0% vs LW" on every new release, which is a claim Mojo never made."""
    assert parse_chart(_fixture_html(), top_n=1)[0].gross_change_pct is None


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("-38%", -38), ("+12%", 12), ("12%", 12), ("0%", 0),
        ("−38%", -38),      # U+2212 minus — the page is not pure ASCII
        ("–38%", -38),      # en dash, the other typographic minus
        ("+1,200%", 1200),       # an awards bump or a re-release
        ("-0.5%", 0), ("<0.1%", 0),
        ("-", None), ("", None), ("n/a", None),
    ],
)
def test_a_percentage_cell_keeps_its_sign_and_its_magnitude(
    cell: str, expected: int | None
) -> None:
    """`_clean_int` cannot serve here: it strips the sign, and the sign IS the fact — a
    film at -38% and one at +38% are opposite stories.

    The decimal cases are the ones a digit-sweeping shortcut gets wrong: it reads "<0.1%"
    as 1% and "-0.5%" as -5%, inventing a move an order of magnitude larger than reported.
    """
    assert _clean_percent(cell) == expected


def test_a_layout_without_the_new_columns_still_yields_a_chart() -> None:
    """Both are optional, exactly as Total Gross is: the four required columns are the
    only ones whose absence is a broken page."""
    html = _fixture_html().replace("Theaters", "Screens").replace("%± LW", "Delta")

    entries = parse_chart(html, top_n=3)

    assert len(entries) == 3
    assert all(e.theaters is None and e.gross_change_pct is None for e in entries)
    assert entries[0].gross_amount == 81_514_000  # ...and the rest still parses


# --- which chart a run fetches, and in whose money (M1) ---


def test_a_region_fetches_the_weekend_chart_with_its_area_code() -> None:
    """Mojo publishes a weekly chart for Domestic only; per region it publishes the
    weekend one. Same week ids and the same headers, so only the path segment differs."""
    assert (
        week_chart_url(BOM_WEEKLY_URL, "2026W33", area="GB")
        == "https://www.boxofficemojo.com/weekend/2026W33/?area=GB"
    )
    assert (
        week_chart_url(BOM_WEEKLY_URL, None, area="GB")
        == "https://www.boxofficemojo.com/weekend/?area=GB"
    )


def test_domestic_urls_are_byte_identical_to_before() -> None:
    """The default path must not move at all: every existing install is on it."""
    assert week_chart_url(BOM_WEEKLY_URL, "2026W33") == (
        "https://www.boxofficemojo.com/weekly/2026W33/"
    )
    assert week_chart_url(BOM_WEEKLY_URL, None) == "https://www.boxofficemojo.com/weekly/"
    assert week_chart_url(BOM_WEEKLY_URL, "2026W33", area=DOMESTIC_REGION) == (
        "https://www.boxofficemojo.com/weekly/2026W33/"
    )


def test_a_custom_base_url_keeps_whatever_was_configured() -> None:
    """Only a trailing `weekly` segment is rewritten, so an operator pointing
    BM_BOXOFFICE_URL at a fixture server keeps their path and simply gains the query."""
    assert week_chart_url("http://fixture.test/chart", "2026W33", area="DE") == (
        "http://fixture.test/chart/2026W33/?area=DE"
    )


def test_the_area_code_is_encoded_not_pasted() -> None:
    """It comes from a closed table today, but it lands in a URL — and a value that
    reaches a URL unencoded is one query parameter away from being two."""
    assert week_chart_url(BOM_WEEKLY_URL, None, area="A&b=c") == (
        "https://www.boxofficemojo.com/weekend/?area=A%26b%3Dc"
    )


def test_the_week_index_is_read_from_either_chart() -> None:
    """`find_latest_week` scans whichever index it was handed; the regional one links
    /weekend/ paths, and matching only /weekly/ would make every regional run fall back
    to the bare page."""
    assert find_latest_week('<a href="/weekend/2026W31/?area=GB">wk</a>') == "2026W31"
    assert find_latest_week('<a href="/weekly/2026W31/">wk</a>') == "2026W31"


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("$81,514,000", "$"), ("£4,209,000", "£"), ("€4.209.000", "€"),
        ("¥1,200,000", "¥"), ("A$3,000,000", "A$"), ("R$9,000,000", "R$"),
        ("-", None), ("1,000", None), ("", None),
    ],
)
def test_the_currency_is_read_off_the_page_not_assumed(
    cell: str, expected: str | None
) -> None:
    """The one fact about a regional chart this app cannot know at build time is what
    currency it prints. A table saying "GB means £" would be a guess, and a wrong guess
    puts the wrong symbol in front of real money — so the symbol is read from the cell.
    """
    assert _currency_prefix(cell) == expected


def test_the_domestic_fixture_reports_its_own_dollars() -> None:
    """Nothing special-cases Domestic: it renders `$` because its page prints `$`."""
    entry = parse_chart(_fixture_html(), top_n=1)[0]

    assert entry.currency_symbol == "$"
    assert format_gross(entry.gross_amount, entry.currency_symbol) == "$81.5M"


def test_a_chart_in_another_currency_formats_in_it() -> None:
    html = _fixture_html().replace("$", "£")

    entry = parse_chart(html, top_n=1)[0]

    assert entry.currency_symbol == "£"
    assert format_gross(entry.gross_amount, entry.currency_symbol) == "£81.5M"
    assert entry.gross_amount == 81_514_000  # the digits parse the same either way


def test_a_european_thousands_separator_still_parses() -> None:
    """`_clean_int` keeps digits only, which is what makes "€4.209.000" work — the dot is
    a separator there, not a decimal point."""
    html = _fixture_html().replace("$81,514,000", "€81.514.000")

    entry = parse_chart(html, top_n=1)[0]

    assert entry.gross_amount == 81_514_000
    assert entry.currency_symbol == "€"


def test_an_unknown_region_reads_as_domestic() -> None:
    """Read-tolerant, so a hand-edited filters.yml naming a region this build dropped
    starts as Domestic instead of refusing to start at all."""
    assert validated_region("GB") == "GB"
    assert validated_region("XX") == DOMESTIC_REGION
    assert validated_region(None) == DOMESTIC_REGION
    assert validated_region(7) == DOMESTIC_REGION
    assert validated_region("") == DOMESTIC_REGION


# --- the release link the parser used to throw away (M5) ---

RELEASE_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "bom_release.html"


def _chart_with_release_href(href: str) -> str:
    """The fixture with rank 1's release link pointed somewhere else."""
    return _fixture_html().replace('href="/release/rl1/"', f'href="{href}"')


def test_the_fixture_yields_the_release_page_each_row_links() -> None:
    """Present in every chart Mojo serves, and discarded at parse time until now."""
    entries = parse_chart(_fixture_html(), top_n=3)

    assert [entry.release_path for entry in entries] == [
        "/release/rl1/", "/release/rl2/", "/release/rl3/",
    ]


@pytest.mark.parametrize("href", [
    "/elsewhere/x/",                         # a path, but not a release page
    "https://www.boxofficemojo.com/release/rl1/",  # absolute, even to the right host
    "//evil.example/release/rl1/",           # protocol-relative
    "http://evil.example/release/rl1/",      # somewhere else entirely
    "/release/rl1/../../etc/passwd",         # traversal
    "/release/",                             # no id at all
    "javascript:alert(1)",                   # not a URL this app would ever follow
])
def test_only_a_release_path_on_mojos_own_host_survives(href: str) -> None:
    """An allow-list, because the value ends up in a URL this app then fetches. Nothing
    that is not `/release/<alphanumeric>/` may reach `urljoin` at all."""
    html = _chart_with_release_href(href)

    assert parse_chart(html, top_n=1)[0].release_path is None


@pytest.mark.parametrize("href", [
    "/release/rl1/?ref_=bo_we_table_1",
    "/release/rl1/#cast",
    "/release/rl1/?ref_=bo_wk#top",
])
def test_mojos_own_tracking_parameter_does_not_lose_the_link(href: str) -> None:
    """Mojo links its own chart rows with a `?ref_=` parameter. A rule that rejected
    those would leave this feature never firing against the real site — and the query is
    dropped rather than carried, so one film keeps one cache entry however it was linked.
    """
    assert parse_chart(_chart_with_release_href(href), top_n=1)[0].release_path == "/release/rl1/"


def test_a_row_that_links_nothing_is_not_an_error() -> None:
    html = _fixture_html().replace(
        '<a class="a-link-normal" href="/release/rl1/">Dune: Part Two</a>',
        "Dune: Part Two",
    )
    entries = parse_chart(html, top_n=1)

    assert entries[0].title == "Dune: Part Two"
    assert entries[0].release_path is None


def test_the_imdb_id_is_read_off_a_release_page() -> None:
    match = RELEASE_IMDB_RE.search(RELEASE_FIXTURE.read_text(encoding="utf-8"))

    assert match is not None
    assert match.group(1) == "tt15239678"


@pytest.mark.parametrize("body,expected", [
    ('<a href="/title/tt1234567/">x</a>', "tt1234567"),      # seven digits
    ('<a href="/title/tt12345678/">x</a>', "tt12345678"),    # eight
    ('<a href="/title/tt123456789/">x</a>', "tt123456789"),  # nine, room to grow
    ('<a href="/title/tt123456/">x</a>', None),              # too short to be an id
    ('<a href="/title/nm1234567/">x</a>', None),             # a person, not a title
    ("no link here at all", None),
])
def test_the_id_pattern_is_bounded_to_what_imdb_issues(body: str, expected: str | None) -> None:
    match = RELEASE_IMDB_RE.search(body)

    assert (match.group(1) if match else None) == expected


def test_an_id_longer_than_the_bound_is_no_id_rather_than_a_truncated_one() -> None:
    """A truncated id is not a near miss — it is a different film's id, which is the
    wrong-poster failure this whole feature exists to stop."""
    assert RELEASE_IMDB_RE.search('<a href="/title/tt1234567890/">x</a>') is None


@respx.mock
async def test_a_release_page_answers_with_its_imdb_id() -> None:
    respx.get("https://www.boxofficemojo.com/release/rl1/").mock(
        return_value=httpx.Response(200, text=RELEASE_FIXTURE.read_text(encoding="utf-8"))
    )
    async with httpx.AsyncClient() as client:
        found = await fetch_release_imdb_id(client, BOM_WEEKLY_URL, "/release/rl1/")

    assert found == "tt15239678"


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(404),
    httpx.Response(500),
    httpx.Response(200, text="<html><body>no id on this page</body></html>"),
])
async def test_a_page_that_cannot_answer_is_not_a_failure(response: httpx.Response) -> None:
    """This runs to make a guess better, so it must never make a run worse."""
    respx.get("https://www.boxofficemojo.com/release/rl1/").mock(return_value=response)
    async with httpx.AsyncClient() as client:
        assert await fetch_release_imdb_id(client, BOM_WEEKLY_URL, "/release/rl1/") is None


@respx.mock
async def test_an_unreachable_release_page_is_not_a_failure() -> None:
    respx.get("https://www.boxofficemojo.com/release/rl1/").mock(
        side_effect=httpx.ConnectError("down")
    )
    async with httpx.AsyncClient() as client:
        assert await fetch_release_imdb_id(client, BOM_WEEKLY_URL, "/release/rl1/") is None


@respx.mock
async def test_a_path_that_is_not_a_release_page_is_never_fetched() -> None:
    """The guard is re-checked at the fetch, not merely at the parse: this is the function
    that builds a URL and requests it."""
    route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="tt1234567"))
    async with httpx.AsyncClient() as client:
        found = await fetch_release_imdb_id(client, BOM_WEEKLY_URL, "http://evil.example/x/")

    assert found is None
    assert route.call_count == 0, "an unvalidated path reached the network"


def test_the_release_lookup_budget_is_small() -> None:
    """Every lookup is another request at Box Office Mojo, and they only ever happen for
    titles Radarr could not recognise on a chart that changed."""
    assert MAX_RELEASE_LOOKUPS_PER_RUN == 5
