"""The core automation loop (Step 14): scrape → match → report.

A pure composition of steps 9–13. The run is user-supervised: it fetches the
chart, matches each title against the Radarr library, and records what it found —
it NEVER adds to Radarr. Adding is a deliberate, per-title action the admin takes
from the weekly view (see `app.web.reports.add_movie`), so re-running a week only
ever refreshes the report — and only when Box Office Mojo has actually changed it,
since a run whose chart matches what is already stored records nothing at all.
Every external call is defensive: a failed scrape, an unreachable Radarr, or a
per-movie error produces a recorded outcome rather than crashing the unattended run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from secrets import token_hex

import httpx

from app.core.audit import AuditAction, AuditLog
from app.core.config import Settings
from app.services import boxoffice
from app.services.apps import AppsStore
from app.services.boxoffice import BoxOfficeEntry, ScrapeError, format_gross
from app.services.corrections import Correction, CorrectionStore
from app.services.ignore import IgnoreSnapshot, IgnoreStore
from app.services.matcher import Candidate, find_match, normalize_title
from app.services.radarr import RadarrError, RadarrLookupResult, RadarrMovie
from app.services.release_ids import ReleaseIdCache
from app.services.reports import (
    MATCHED_BY_GUESS,
    MAX_REPORTS,
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    imdb_url,
    wiki_url,
)

SCRAPE_FAILURE_SUBDIR = "scrape-failures"
# How many genres a card carries. The card is 208px wide and the line has to survive a
# longer translation of each name, so this is a display bound, not a data one.
CARD_GENRES = 3

# What a week's chart says, with nothing about the Radarr library in it. Two runs that
# produce the same fingerprint found the same chart, so the second has nothing to record.
ChartFingerprint = list[tuple[int, str, int, int, int | None, int | None]]

# Returns (resolved_week_id, entries) — the week actually parsed may differ from the
# requested one when we step back over an in-progress week.
FetchChart = Callable[[str | None], Awaitable[tuple[str, list[BoxOfficeEntry]]]]
# Read per run, not per construction: changing the depth in Settings must take effect on
# the next run without restarting the app or handing the pipeline a settings store.
ChartSize = Callable[[], int]
# Same arrangement for how many weeks are kept: read per run so a change in Settings
# applies to the next one without a restart.
ReportKeep = Callable[[], int]
# And for which chart is fetched, for the same reason.
Region = Callable[[], str]
MakeRadarr = Callable[[str], object]
SelectAppId = Callable[[], str | None]
# Confirming a guessed title: a release path in, the film's IMDb id or None out. Injected
# for the same reason `fetch_chart` is — so a test can answer without a network.
FetchReleaseId = Callable[[str], Awaitable[str | None]]


class _ReleaseLookups:
    """How many release pages this run may still fetch.

    Per run rather than per pipeline: a backfill and a manual run can be in flight at the
    same time, and an instance counter would let one spend the other's budget — or, worse,
    hand a long backfill a budget that never resets.
    """

    def __init__(self, limit: int) -> None:
        self._remaining = limit

    def take(self) -> bool:
        """Claim one lookup. False once the run has spent its budget."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


class Pipeline:
    def __init__(
        self,
        *,
        apps: AppsStore,
        reports_store: ReportsStore,
        settings: Settings,
        audit: AuditLog,
        ignore_store: IgnoreStore,
        fetch_chart: FetchChart | None = None,
        make_radarr: MakeRadarr | None = None,
        select_app_id: SelectAppId | None = None,
        chart_size: ChartSize | None = None,
        report_keep: ReportKeep | None = None,
        region: Region | None = None,
        corrections: CorrectionStore | None = None,
        release_ids: ReleaseIdCache | None = None,
        fetch_release_id: FetchReleaseId | None = None,
    ) -> None:
        self._apps = apps
        self._reports = reports_store
        self._settings = settings
        self._audit = audit
        self._ignore = ignore_store
        self._fetch_chart = fetch_chart or self._default_fetch_chart
        self._make_radarr = make_radarr or self._default_make_radarr
        self._select_app_id = select_app_id or self._default_select_app_id
        self._chart_size = chart_size or (lambda: boxoffice.TOP_N)
        self._report_keep = report_keep or (lambda: MAX_REPORTS)
        self._region = region or (lambda: boxoffice.DOMESTIC_REGION)
        self._corrections = corrections or CorrectionStore(settings.config_dir)
        self._release_ids = release_ids or ReleaseIdCache(settings.cache_dir)
        self._fetch_release_id = fetch_release_id or self._default_fetch_release_id

    async def _default_fetch_chart(self, week: str | None) -> tuple[str, list[BoxOfficeEntry]]:
        return await boxoffice.fetch_weekly_chart(
            snapshot_dir=self._settings.logs_dir / SCRAPE_FAILURE_SUBDIR,
            url=self._settings.boxoffice_url,
            week=week,
            top_n=self._chart_size(),
            area=self._region(),
        )

    async def _default_fetch_release_id(self, release_path: str) -> str | None:
        """One release page, on its own short-lived client.

        Its own rather than the chart's: this happens per guessed title, long after the
        chart fetch has closed, and a run makes at most five of them.
        """
        async with httpx.AsyncClient(
            headers={"User-Agent": boxoffice.USER_AGENT},
            timeout=boxoffice.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            return await boxoffice.fetch_release_imdb_id(
                client, self._settings.boxoffice_url, release_path
            )

    def _default_make_radarr(self, app_id: str) -> object:
        return self._apps.build_client(
            app_id,
            tls_verify=self._settings.outbound_tls_verify,
            ca_file=str(self._settings.tls_ca_file) if self._settings.tls_ca_file else None,
        )

    def _default_select_app_id(self) -> str | None:
        return self._apps.primary_id()

    def _unchanged_report(
        self, week: str, entries: list[BoxOfficeEntry]
    ) -> Report | None:
        """The stored report this chart would duplicate, if there is one.

        Compares what Box Office Mojo said, not what Radarr said: the library moves
        constantly and the weekly view re-reads it live on every render, so a difference
        there is not a reason to write a second report of the same chart.

        Deliberately a content check rather than "has this week been fetched before".
        Mojo publishes estimates and finalises them days later, so the same week really
        does change; skipping on existence alone would pin a week to its first, roughest
        numbers forever.
        """
        stored = self._reports.latest_for_week(week)
        if stored is None:
            return None
        # A stored week fetched from a DIFFERENT chart is not this chart, whatever its
        # numbers happen to be. Compared here rather than folded into the fingerprint,
        # which is a per-title shape and has no room for a fact about the whole run.
        if stored.region != self._region():
            return None
        return stored if _fingerprint_of(stored) == _fingerprint(entries) else None

    async def run(self, *, trigger: str, week: str | None = None) -> Report:
        run_at = datetime.now(UTC)
        report_id = f"report-{run_at:%Y%m%d-%H%M%S}-{token_hex(2)}"
        week_label = week or "current"

        app_id = self._select_app_id()
        if app_id is None:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.NO_APP,
                "No Radarr connection is configured.",
            )

        try:
            resolved_week, entries = await self._fetch_chart(week)
        except ScrapeError as exc:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.SCRAPE_FAILED, str(exc)
            )
        # Label the report with the week actually fetched (a picked/current week may have
        # stepped back over an in-progress one), so the report and its re-run are honest.
        week_label = resolved_week

        # Before touching Radarr: if this chart is what we already stored for the week,
        # there is nothing to record. Whoever fetched it first — the scheduler or the
        # admin — keeps the report, and the second attempt is a no-op rather than a
        # duplicate. Checked here so an unchanged week costs one page fetch and no
        # library read, lookup or poster work at all.
        unchanged = self._unchanged_report(week_label, entries)
        if unchanged is not None:
            self._audit.record(
                AuditAction.PIPELINE_RUN, trigger=trigger, week=week_label,
                movies=len(entries), unchanged=True,
            )
            return unchanged

        try:
            radarr = self._make_radarr(app_id)
            library = await radarr.list_movies()
        except KeyError:
            # The connection was deleted between selecting it and building the client
            # (AppNotFoundError subclasses KeyError) — report it rather than crash.
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.NO_APP,
                "The Radarr connection was removed while the run was in progress.",
            )
        except RadarrError as exc:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.RADARR_FAILED,
                f"Could not read the Radarr library: {exc}",
            )

        library_by_tmdb = {movie.tmdb_id: movie for movie in library}

        # One read for the whole run, so every chart entry is judged against the same
        # list even if the admin ignores something while the run is in flight.
        ignored = self._ignore.snapshot()
        # Read once for the whole run, for the same reason the ignore list is: every row
        # is then judged against the same answers, rather than against whatever the file
        # held at the moment that row happened to be built.
        corrections = self._corrections.all()
        # One budget for the whole run, so five guessed titles cost five release pages and
        # a sixth costs none. Reached only on a chart that changed — the unchanged check
        # above returns before any of this.
        lookups = _ReleaseLookups(boxoffice.MAX_RELEASE_LOOKUPS_PER_RUN)
        results = [
            await self._reconcile(entry, radarr, library_by_tmdb, ignored, corrections, lookups)
            for entry in entries
        ]
        matched = sum(
            1 for result in results if result.status in (MovieStatus.IN_LIBRARY, MovieStatus.WANTED)
        )
        # A week keeps its report id when it is fetched again, so this is an update
        # rather than a second card — and every link to it stays valid: the week chips on
        # the search results and the ignored list, and any bookmark.
        stored = self._reports.latest_for_week(week_label)
        report = Report(
            id=stored.id if stored is not None else report_id,
            run_at=run_at.isoformat(),
            trigger=trigger,
            status=RunStatus.OK,
            week=week_label,
            region=self._region(),
            currency=_chart_currency(entries),
            totals=ReportTotals(movies=len(results), matched=matched),
            movies=results,
        )
        self._reports.replace_week(report)
        self._reports.prune(self._report_keep())
        self._audit.record(
            AuditAction.PIPELINE_RUN, trigger=trigger, week=week_label,
            movies=len(results), matched=matched,
        )
        return report

    async def _reconcile(
        self,
        entry: BoxOfficeEntry,
        radarr: object,
        library_by_tmdb: dict[int, RadarrMovie],
        ignored: IgnoreSnapshot,
        corrections: dict[str, Correction],
        lookups: _ReleaseLookups,
    ) -> MovieResult:
        """Report each chart title's status against the library. NEVER adds.

        Adding to Radarr is a deliberate, per-title action the admin takes from the
        weekly view — a run only fetches, matches, and records what it found.
        """
        common = {
            "rank": entry.rank,
            "title": entry.title,
            "normalized_title": normalize_title(entry.title),
            "gross_amount": entry.gross_amount,
            # The symbol this row's own page printed, not one this app decided on. A
            # regional chart's figures are that region's money, and the display string is
            # frozen here at write time — which is why a stored week keeps its currency
            # when the region setting later changes.
            "gross_display": format_gross(
                entry.gross_amount, entry.currency_symbol or boxoffice.DEFAULT_CURRENCY_SYMBOL
            ),
            "total_gross": entry.total_gross,
            "theaters": entry.theaters,
            "gross_change_pct": entry.gross_change_pct,
            "weeks_in_release": entry.weeks_in_release,
        }

        try:
            candidates_raw: list[RadarrLookupResult] = await radarr.lookup(entry.title)
        except RadarrError as exc:
            return MovieResult(
                **common, status=MovieStatus.MISSING, action=MovieAction.ERROR,
                detail=f"lookup failed: {exc}",
            )

        # Before anything this run could work out for itself: an admin has already looked
        # at this chart title and said which film it is. That outranks a title match, an
        # IMDb confirmation and certainly a guess — and it is checked before the empty
        # result below, because a title Radarr's search cannot find at all is exactly the
        # kind that got corrected by hand in the first place.
        correction = corrections.get(common["normalized_title"])
        if correction is not None:
            common["title"] = correction.title
            return self._result(
                common, _links_from_correction(correction, candidates_raw),
                library_by_tmdb, ignored,
            )

        if not candidates_raw:
            return MovieResult(**common, status=MovieStatus.MISSING, action=MovieAction.NO_MATCH)

        candidates = [Candidate(item.title, item.year, item) for item in candidates_raw]
        matched = find_match(entry.title, candidates)
        # Nothing Radarr returned matched the title. Before falling back to its first
        # suggestion — which is how a remake, a sequel or a re-release ends up on the wrong
        # card — ask Mojo which film this row actually is. An IMDb id is exact where a
        # title is not, and both sides already have one.
        confirmed = (
            None if matched else await self._confirm_by_imdb(entry, candidates_raw, lookups)
        )
        # Still a guess only when neither route identified it. That case keeps the amber
        # hint it has always had; the fix-match flow is what it is for.
        best: RadarrLookupResult = confirmed or matched or candidates_raw[0]
        links = {
            "tmdb_id": best.tmdb_id,
            "year": best.year,
            "poster_url": best.poster_url,
            "imdb_url": imdb_url(best.imdb_id),
            "wiki_url": wiki_url(entry.title),
            # Already in hand from the lookup this run just made — the old code fetched
            # them and threw them away, leaving the card to be judged on rank and gross.
            "rating": best.rating,
            "genres": list(best.genres)[:CARD_GENRES],
            # Carried on every matched path, including the in-library one: how the film
            # was identified is a fact about the match, not about the library. Recording
            # it only while missing would silently drop it the moment someone adds it.
            "detail": None if (matched or confirmed) else MATCHED_BY_GUESS,
        }
        return self._result(common, links, library_by_tmdb, ignored)

    def _result(
        self,
        common: dict,
        links: dict,
        library_by_tmdb: dict[int, RadarrMovie],
        ignored: IgnoreSnapshot,
    ) -> MovieResult:
        """One identified row, judged against the library and the ignore list.

        Shared by every path that ends with a film in hand, so a correction and a match
        cannot drift into answering "is this in the library" two different ways.
        """
        in_library = library_by_tmdb.get(links["tmdb_id"])
        if in_library is not None:
            status = MovieStatus.IN_LIBRARY if in_library.has_file else MovieStatus.WANTED
            return MovieResult(**common, **links, status=status, action=MovieAction.NONE)

        # Missing from the library. Flag ignored titles; everything else is simply
        # available for the admin to add manually — no auto-add.
        if ignored.is_ignored(links["tmdb_id"], common["normalized_title"]):
            return MovieResult(
                **common, **links, status=MovieStatus.MISSING, action=MovieAction.IGNORED
            )
        return MovieResult(
            **common, **links, status=MovieStatus.MISSING, action=MovieAction.NONE
        )

    async def _confirm_by_imdb(
        self,
        entry: BoxOfficeEntry,
        candidates: list[RadarrLookupResult],
        lookups: _ReleaseLookups,
    ) -> RadarrLookupResult | None:
        """The candidate whose IMDb id is the one Mojo's release page names, or None.

        None for every ordinary disappointment — the row linked no page, the run has spent
        its budget, the page could not be read, or no candidate carries that id. This
        exists to improve a guess, never to endanger a run, so every one of those simply
        leaves the guess exactly as it was.
        """
        if entry.release_path is None:
            return None
        imdb_id = self._release_ids.get(entry.release_path)
        if imdb_id is None:
            if not lookups.take():
                return None
            imdb_id = await self._fetch_release_id(entry.release_path)
            if imdb_id is None:
                return None
            # Written only on success: a page that failed to answer is worth asking again,
            # while an id, once known, is true forever.
            self._release_ids.put(entry.release_path, imdb_id)
        return next(
            (candidate for candidate in candidates if candidate.imdb_id == imdb_id), None
        )

    def _save_failed(
        self, report_id: str, run_at: datetime, trigger: str, week: str, status: str, error: str
    ) -> Report:
        report = Report(
            id=report_id,
            run_at=run_at.isoformat(),
            trigger=trigger,
            status=status,
            week=week,
            region=self._region(),
            totals=ReportTotals(movies=0, matched=0),
            error=error,
        )
        self._reports.save(report)
        self._reports.prune(self._report_keep())
        self._audit.record(AuditAction.PIPELINE_RUN, trigger=trigger, week=week, status=status)
        return report


def _chart_currency(entries: list[BoxOfficeEntry]) -> str:
    """The symbol this chart printed, from the first row that carried one.

    One per report rather than per title: every row of a chart is the same market's money,
    and the aggregations built on stored history need one answer per week to compare.
    """
    for entry in entries:
        if entry.currency_symbol:
            return entry.currency_symbol
    return boxoffice.DEFAULT_CURRENCY_SYMBOL


def _links_from_correction(
    correction: Correction, candidates: list[RadarrLookupResult]
) -> dict:
    """The card's film, as an admin confirmed it.

    The confirmation carries its own identity and artwork, so this holds even when
    Radarr's search no longer returns that film for this chart title — which is the case
    a correction most often exists for. The rating and genres are the one thing taken from
    the live lookup when it happens to include the film: they drift on their own, and a
    year-old snapshot of a score is worse than none.

    No `detail`: a human compared two posters and chose. Nothing this app works out for
    itself is a stronger answer than that.
    """
    fresh = next((item for item in candidates if item.tmdb_id == correction.tmdb_id), None)
    return {
        "tmdb_id": correction.tmdb_id,
        "year": correction.year,
        "poster_url": correction.poster_url,
        "imdb_url": correction.imdb_url,
        "wiki_url": wiki_url(correction.title),
        "rating": fresh.rating if fresh else None,
        "genres": list(fresh.genres)[:CARD_GENRES] if fresh else [],
        "detail": None,
    }


def _fingerprint(entries: list[BoxOfficeEntry]) -> ChartFingerprint:
    """The chart as scraped, in the shape a stored report can be compared against.

    Only what Box Office Mojo said. Deliberately not the rating or the genres, which come
    from Radarr and drift on their own: folding them in would make an unchanged week look
    changed every time a TMDB score moved a tenth, and rewrite a report for nothing.

    Theaters IS in here: it is Mojo's own count, and it is revised from estimate to actual
    exactly as the grosses are, so a week whose screen counts firmed up has genuinely
    changed. The week-over-week percentage is NOT, and for the opposite reason — Mojo
    derives it from the gross figures already fingerprinted here, so including it would
    only report the same change twice.
    """
    return [
        (
            entry.rank,
            normalize_title(entry.title),
            entry.gross_amount,
            entry.weeks_in_release,
            entry.total_gross,
            entry.theaters,
        )
        for entry in entries
    ]


def _fingerprint_of(report: Report) -> ChartFingerprint:
    """The same shape, read back out of a stored report.

    Reports written before a field existed carry None for it, so the first run after an
    upgrade sees a difference and records the fuller chart — which is what should happen.
    """
    return [
        (
            movie.rank,
            movie.normalized_title,
            movie.gross_amount,
            movie.weeks_in_release,
            movie.total_gross,
            movie.theaters,
        )
        for movie in report.movies
    ]
