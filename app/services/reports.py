"""Weekly run reports — the JSON records the pipeline writes and the UI reads.

One file per run under `/data/history/report-<ts>.json`. The format is stable
(schema-versioned) so backups and the reports UI (Step 17) never need to change
when the pipeline internals do.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, Field

from app.core import filestore
from app.services.boxoffice import CURRENT_WEEK, previous_week_id, week_start

REPORT_SCHEMA_VERSION = 1
REPORT_FILENAME_PREFIX = "report-"
REPORT_FILENAME_SUFFIX = ".json"
# Default retention for run reports, and the bounds the Settings field allows.
#
# The cap exists because /data/history is read WHOLE on every dashboard and reports view,
# so retention is a page-load setting as much as a storage one: measured at 4ms for 50
# reports, 17ms for 260 and 76ms for 1000. The ceiling is five years of weekly reports —
# past that a page pays for history nobody is reading.
#
# The floor keeps the features built on that history meaningful: the month leaderboard,
# the trend lines and the dashboard's "weeks tracked" all read it.
MAX_REPORTS = 50
MIN_REPORT_RETENTION = 10
MAX_REPORT_RETENTION = 260
# How many holes one backfill offers to fill. Scrape politeness, the same kind of ceiling
# as MAX_CHART_SIZE: every week is another page fetched from Box Office Mojo and another
# round of Radarr lookups. A history with more holes than this takes a second click.
MAX_BACKFILL_WEEKS = 12
IMDB_TITLE_URL = "https://www.imdb.com/title/{imdb_id}/"
WIKI_SEARCH_URL = "https://en.wikipedia.org/wiki/Special:Search?search={query}"
_REPORT_ID_RE = re.compile(r"^report-[0-9A-Za-z\-]+$")


class MovieStatus:
    IN_LIBRARY = "in_library"
    WANTED = "wanted"
    MISSING = "missing"


class MovieAction:
    NONE = "none"
    ADDED = "added"
    NO_MATCH = "no_match"
    IGNORED = "ignored"
    ERROR = "error"


# Radarr answered the lookup but nothing it returned actually matched the chart title, so
# the run took its first suggestion. A `detail` value rather than a MovieAction: an action
# drives what the card offers to DO, and this only changes what the card admits.
MATCHED_BY_GUESS = "matched_by_guess"


class RunStatus:
    OK = "ok"
    SCRAPE_FAILED = "scrape_failed"
    NO_APP = "no_app"
    RADARR_FAILED = "radarr_failed"


class RunTrigger:
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class MovieResult(BaseModel):
    rank: int
    title: str
    normalized_title: str
    gross_amount: int
    gross_display: str
    # The film's running box-office total, as Mojo reports it beside the weekly figure.
    # Optional and additive, so REPORT_SCHEMA_VERSION does not move: an older build reads
    # a newer file by ignoring it, and a newer build reads an older file as None. Reports
    # written before this existed simply have no total to show.
    total_gross: int | None = None
    # What Radarr's own lookup already reported for the matched film, kept so the weekly
    # card can be decided on more than rank and gross. Additive with defaults, exactly as
    # total_gross above: REPORT_SCHEMA_VERSION does not move, an older build ignores them
    # and a newer build reads an older report as unrated and un-genred.
    #
    # The overview is deliberately NOT stored. The movie modal fetches it live, no card
    # has room for it, and a few hundred characters per title would roughly double every
    # report file — which every page parses whole.
    rating: float | None = Field(default=None, ge=0)
    genres: list[str] = Field(default_factory=list)
    # What Mojo says about the film's week beyond its take: how many screens, and how that
    # take moved against the previous week. Additive with defaults exactly as the fields
    # above are, so REPORT_SCHEMA_VERSION does not move and a report written before they
    # existed simply has neither.
    theaters: int | None = Field(default=None, ge=0)
    gross_change_pct: int | None = None
    weeks_in_release: int
    status: str
    action: str
    tmdb_id: int | None = None
    year: int | None = None
    poster_url: str | None = None
    imdb_url: str | None = None
    wiki_url: str | None = None
    detail: str | None = None


def wiki_url(title: str) -> str:
    """A Wikipedia search for a film title.

    Beside `imdb_url` and for the same reason: it is derived from the title, so the
    pipeline and the manual match fixer must build it the same way. Living in the
    pipeline is what let a corrected match keep pointing at the old film's article.
    """
    return WIKI_SEARCH_URL.format(query=quote(title))


def imdb_url(imdb_id: str | None) -> str | None:
    """The IMDb page for a Radarr lookup result, or None when it has no IMDb id.
    Lives here beside MovieResult so the pipeline and the manual match fixer agree."""
    return IMDB_TITLE_URL.format(imdb_id=imdb_id) if imdb_id else None


class ReportTotals(BaseModel):
    movies: int
    matched: int


class Report(BaseModel):
    id: str
    run_at: str
    trigger: str
    status: str
    totals: ReportTotals
    week: str = "current"  # BOM week id (e.g. "2026W03") or "current"
    # Which chart this week came from, and the symbol its own page printed. Additive with
    # defaults so REPORT_SCHEMA_VERSION does not move: a report written before regions
    # existed reads back as domestic dollars, which is exactly what it was. A stored week
    # keeps what it was fetched with — changing the region changes future fetches, and
    # Re-run is how an old week is brought over.
    region: str = ""
    currency: str = "$"
    movies: list[MovieResult] = Field(default_factory=list)
    error: str | None = None


class ReportsStore:
    def __init__(self, history_dir: Path) -> None:
        self._dir = history_dir
        # Files already reported as unreadable. `list_reports` runs on every page view,
        # so without this one bad file prints on every request and rotates the real
        # diagnostics out of `docker logs` — the same flooding `app.core.audit` bounds
        # `actor` to avoid. Only ever grows with DISTINCT broken files.
        self._warned_unreadable: set[str] = set()

    def _path(self, report_id: str) -> Path:
        if not _REPORT_ID_RE.match(report_id):
            raise ValueError(f"invalid report id: {report_id!r}")
        return self._dir / f"{report_id}{REPORT_FILENAME_SUFFIX}"

    def save(self, report: Report) -> None:
        filestore.write_json(
            self._path(report.id), report.model_dump(), schema_version=REPORT_SCHEMA_VERSION
        )

    def list_reports(self) -> list[Report]:
        """Every stored report, newest first. One unreadable file is skipped, not fatal.

        This feeds the dashboard, the reports list, the trend histories and the
        poster-prune keep-set, so a single corrupt or future-versioned file used to take
        all of them down together. A skipped file is left on disk for diagnosis rather
        than deleted — it is the evidence.
        """
        if not self._dir.exists():
            return []
        reports: list[Report] = []
        for path in self._dir.glob(f"{REPORT_FILENAME_PREFIX}*{REPORT_FILENAME_SUFFIX}"):
            try:
                document = filestore.read_json(
                    path, expected_version=REPORT_SCHEMA_VERSION
                )
                document.pop(filestore.SCHEMA_VERSION_KEY, None)
                reports.append(Report.model_validate(document))
            except FileNotFoundError:
                # `prune` deleted it between the glob and the read. The sync routes run
                # in the threadpool while a scheduled run prunes, so this races for real
                # — and a file that is simply gone leaves nothing to diagnose.
                continue
            except (ValueError, filestore.SchemaVersionError) as exc:
                # pydantic's ValidationError and json's JSONDecodeError are both
                # ValueError; SchemaVersionError is too, named for the reader.
                self._warn_unreadable(path.name, exc)
        # Newest first (ids are timestamp-prefixed).
        reports.sort(key=lambda report: report.run_at, reverse=True)
        return reports

    def _warn_unreadable(self, name: str, exc: Exception) -> None:
        """Say it once per file per process: loud enough to find, quiet enough to keep."""
        if name in self._warned_unreadable:
            return
        self._warned_unreadable.add(name)
        print(f"skipping unreadable report {name}: {exc}", flush=True)

    def replace_week(self, report: Report) -> None:
        """Store a completed week's report as THE report for that week.

        A week is one thing however often it is fetched. Box Office Mojo revises its
        figures for days after publication, so re-fetching is how a week gets *better* —
        it is not a second week. Anything else stored for it is removed, which also
        collapses duplicates left by earlier versions of this app.
        """
        self.save(report)
        self.collapse_duplicate_weeks()

    def collapse_duplicate_weeks(self) -> int:
        """Leave one completed report per week — the freshest. Returns how many went.

        Failed runs are left alone. A failure records an attempt that did not happen,
        not a chart, so it neither replaces a week's data nor is replaced by it; deleting
        one would hide that a run failed.
        """
        seen: set[str] = set()
        removed = 0
        for report in self.list_reports():  # newest run first, so the keeper comes first
            if report.status != RunStatus.OK:
                continue
            if report.week in seen:
                self.delete(report.id)
                removed += 1
                continue
            seen.add(report.week)
        return removed

    def latest_for_week(self, week: str) -> Report | None:
        """The freshest completed report for one week, or None if there is not one.

        Only OK runs count: a week whose last attempt failed has nothing worth comparing
        against, and must not stop the next attempt from writing a report.
        """
        for report in self.list_reports():  # newest run first
            if report.week == week and report.status == RunStatus.OK:
                return report
        return None

    def missing_weeks(self, reports: list[Report] | None = None) -> list[str]:
        """Weeks inside the stored range that have no report at all, oldest first.

        "Missing" means never attempted, not "has no usable chart". A week that WAS
        fetched and failed — Mojo had nothing for it, or the scrape broke — keeps its
        failed report and its card, and is not offered again. Counting only completed
        weeks here would make a week Box Office Mojo genuinely has no data for come back
        as a hole on every visit, and be re-fetched on every backfill, forever: an endless
        polite retry is still an endless retry. Re-running such a week stays a deliberate,
        per-week action on its own card.

        Bounded by the range actually held, so this never proposes fetching backwards into
        weeks that predate the install. Capped for the same reason the chart depth is.
        """
        attempted = {
            report.week
            for report in (reports if reports is not None else self.list_reports())
            if report.week != CURRENT_WEEK and week_start(report.week) is not None
        }
        if len(attempted) < 2:
            return []  # a single week has no inside to have holes in
        newest, oldest = max(attempted), min(attempted)
        holes: list[str] = []
        week = previous_week_id(newest)
        while week is not None and week > oldest:
            if week not in attempted:
                holes.append(week)
            week = previous_week_id(week)
        holes.sort()
        # The OLDEST gaps first: filling from the far end makes the history contiguous
        # from a fixed point rather than leaving a moving frontier, and a second click
        # takes the next twelve.
        return holes[:MAX_BACKFILL_WEEKS]


    def completed_weeks(self, reports: list[Report] | None = None) -> list[Report]:
        """One report per real week that finished, newest week first.

        Three things every "which weeks did this title chart in" question needs, and all
        three are easy to get subtly wrong: failed runs carry no usable movies, the
        unresolved "current" label is not a week anyone can be sent to, and a re-run
        writes a second report for a week that must not be counted twice.

        Ordered by WEEK rather than by when the report was written — re-running an old
        week today makes its report the newest, and it still belongs at the end of the run.
        """
        seen_weeks: set[str] = set()
        weeks: list[Report] = []
        for report in reports if reports is not None else self.list_reports():
            if report.status != RunStatus.OK or report.week == CURRENT_WEEK:
                continue
            if report.week in seen_weeks:
                continue  # reports arrive newest-run first, so the freshest one wins
            seen_weeks.add(report.week)
            weeks.append(report)
        weeks.sort(key=lambda report: report.week, reverse=True)
        return weeks

    def histories(
        self, reports: list[Report] | None = None
    ) -> dict[str, list[tuple[str, int, int, str]]]:
        """Every title's week-by-week `(week, rank, gross, currency)`, oldest first.

        The currency travels WITH the figure because a history can legitimately mix them:
        the region is a setting, and a week keeps whatever it was fetched with. A gross
        without its currency is a number that cannot be added to anything safely.

        Built for ALL titles in a single pass — a page showing ten movies must not scan
        the history ten times. Callers that already hold the report list pass it in so the
        directory is read once per request. Only completed runs count; a re-run of a week
        is deduplicated (reports arrive newest-first, so the freshest wins), and the
        unresolved "current" label is skipped since it isn't a real week.
        """
        reports = self.list_reports() if reports is None else reports
        seen_weeks: set[str] = set()
        per_title: dict[str, list[tuple[str, int, int, str]]] = {}
        for report in reports:
            if report.status != RunStatus.OK or report.week == CURRENT_WEEK:
                continue
            if report.week in seen_weeks:
                continue
            seen_weeks.add(report.week)
            for movie in report.movies:
                per_title.setdefault(movie.normalized_title, []).append(
                    (report.week, movie.rank, movie.gross_amount, report.currency)
                )
        for entries in per_title.values():
            entries.reverse()  # chronological reads better than newest-first
        return per_title

    def get(self, report_id: str) -> Report:
        document = filestore.read_json(
            self._path(report_id), expected_version=REPORT_SCHEMA_VERSION
        )
        document.pop(filestore.SCHEMA_VERSION_KEY, None)
        return Report.model_validate(document)

    def normalized_title_at(self, report_id: str, rank: int) -> str:
        """The chart title of one row, by the stable per-report key.

        Chart ranks are unique within a run. Read rather than accepted from the browser,
        because this is the key a correction is filed under and everything else keys off
        that in turn.
        """
        for movie in self.get(report_id).movies:
            if movie.rank == rank:
                return movie.normalized_title
        raise KeyError(f"no movie ranked {rank} in {report_id}")

    def apply_correction(self, normalized_title: str, correction: object) -> int:
        """Re-point every row of that chart title at the confirmed film. Returns how many.

        Every stored week, not one row: the same film charts for weeks at a time, and a
        correction that reached only the week the admin happened to be looking at left the
        others showing the wrong poster — and the dashboard showing the film twice.

        `normalized_title` is deliberately NOT rewritten to follow the new title. It is the
        identity of the CHART ROW, not of the film: the week's dedupe fingerprint, the
        ignore key and the cross-week grouping that draws the trend line all read it. When
        it followed the display title, one correction made the week look changed to the
        next automatic check, which then overwrote the correction with a fresh guess — and
        split the film's history in two on the way. `wiki_url` does follow the title,
        because it is a link about the film.

        `detail` is cleared: a human compared two posters and chose, which is a stronger
        answer than any marker this app puts on a guess.
        """
        changed = 0
        for report in self.list_reports():
            touched = False
            for index, movie in enumerate(report.movies):
                if movie.normalized_title != normalized_title:
                    continue
                report.movies[index] = movie.model_copy(
                    update={
                        "tmdb_id": correction.tmdb_id,
                        "title": correction.title,
                        "year": correction.year,
                        "poster_url": correction.poster_url,
                        "imdb_url": correction.imdb_url,
                        "wiki_url": wiki_url(correction.title),
                        "action": MovieAction.NONE,
                        "detail": None,
                    }
                )
                touched = True
            if touched:
                self.save(report)
                changed += 1
        return changed

    def delete(self, report_id: str) -> bool:
        """Remove one report. True if it was there, False if it already wasn't.

        Still raises ValueError for an id that fails `_REPORT_ID_RE` — that check is what
        keeps a crafted id from naming a path outside the history directory, so it stays
        a hard failure rather than a quiet False.
        """
        path = self._path(report_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def prune(self, keep: int = MAX_REPORTS) -> None:
        """Delete all but the newest `keep` reports (newest-first ordering)."""
        for report in self.list_reports()[keep:]:
            self.delete(report.id)

    def latest(self) -> Report | None:
        reports = self.list_reports()
        return reports[0] if reports else None
