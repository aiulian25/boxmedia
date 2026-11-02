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

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_hex

import httpx

from app.core.audit import AuditAction, AuditLog
from app.core.config import Settings
from app.services import boxoffice
from app.services.apps import AppsStore
from app.services.boxoffice import (
    BoxOfficeEntry,
    ChartScope,
    ScrapeError,
    WeekNotPublishedError,
    format_gross,
)
from app.services.corrections import Correction, CorrectionBook, CorrectionStore
from app.services.ignore import IgnoreSnapshot, IgnoreStore
from app.services.matcher import Candidate, find_match, normalize_title
from app.services.radarr import RadarrError, RadarrLookupResult, RadarrMovie
from app.services.release_ids import ReleaseIdCache
from app.services.reports import (
    MATCHED_AMBIGUOUS,
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
ChartFingerprint = list[tuple[int, str, int, int | None, int | None, int | None]]

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


# How many chart titles are looked up at once. Each one is a Radarr lookup — which
# Radarr forwards to its own metadata service — and sometimes a Mojo release page.
# Measured on 20 titles against a real Radarr: 1,884 ms one at a time, 1,066 ms two at
# a time, 696 ms at six, and no better beyond that (727 ms with all twenty at once).
# Six is also how many connections a browser opens to one host, which keeps this as
# polite to Radarr's upstream and to Mojo as a person clicking through would be.
RECONCILE_CONCURRENCY = 6

# The chart facts of a row, which a correction never changes, except for the display title.
_CHART_FIELDS = (
    "rank",
    "title",
    "normalized_title",
    "gross_amount",
    "gross_display",
    "total_gross",
    "theaters",
    "gross_change_pct",
    "weeks_in_release",
    "release_path",
    "source_imdb_id",
)
# How Radarr's search takes an exact id, for a film its title search cannot find.
RADARR_IMDB_TERM = "imdb:{imdb_id}"


class PipelineBusyError(Exception):
    """Another run still held its turn when the caller's patience ran out. Nothing ran."""


@dataclass(frozen=True)
class _RunConfig:
    """The settings one run answers to, read once so an edit applies to the next run: a
    region changed mid-flight must not relabel a chart fetched from the old one."""

    region: str
    report_keep: int
    chart_size: int


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
        # One run at a time, whoever asked. Without it a slow older run could write over a
        # newer run's figures: atomic files stop a torn write, not a stale one. Held for the
        # whole run, so the queue is bounded by who can ask: one job per schedule, one
        # backfill, and manual clicks, which give up. Inside `exclusive` in lock order: a
        # restore waits for whoever holds this, and nothing here ever waits for a restore.
        self._admission = asyncio.Lock()

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
            **self._settings.outbound_tls(),
        )

    def _default_select_app_id(self) -> str | None:
        return self._apps.primary_id()

    def _unchanged_report(
        self, week: str, entries: list[BoxOfficeEntry], config: _RunConfig
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
        # A stored week fetched from a DIFFERENT chart, or printed in different money, is not
        # this chart, whatever its numbers happen to be. Compared here rather than folded into
        # the fingerprint, which is a per-title shape and has no room for a fact about the
        # whole run.
        if stored.chart_scope != ChartScope(config.region, _chart_currency(entries)):
            return None
        return stored if _fingerprint_of(stored) == _fingerprint(entries) else None

    def _shrank(self, week: str, entries: list[BoxOfficeEntry], config: _RunConfig) -> bool:
        """Whether this chart is a truncated view of one already stored.

        A page cut short at a row boundary parses perfectly, with ranks 1 to N and every
        figure present, so the parser cannot tell. Only the stored week can. A lowered depth
        is not this: the chart then still holds as many titles as were asked for.
        """
        stored = self._reports.latest_for_week(week)
        if stored is None or stored.region != config.region:
            return False
        return len(entries) < len(stored.movies) and len(entries) < config.chart_size

    async def run(
        self,
        *,
        trigger: str,
        week: str | None = None,
        admission_timeout: float | None = None,
    ) -> Report:
        """Run once this pipeline is free. `admission_timeout` is how long to wait for that
        before raising PipelineBusyError; None waits, which is right for unattended work,
        whose week would otherwise simply be lost. Cancelling, at any point, gives the turn
        back."""
        try:
            await asyncio.wait_for(self._admission.acquire(), admission_timeout)
        except TimeoutError as exc:
            raise PipelineBusyError("another run is still in progress") from exc
        try:
            return await self._run_admitted(trigger, week)
        finally:
            self._admission.release()

    async def _run_admitted(self, trigger: str, week: str | None) -> Report:
        run_at = datetime.now(UTC)
        config = _RunConfig(
            region=self._region(), report_keep=self._report_keep(), chart_size=self._chart_size()
        )
        report_id = f"report-{run_at:%Y%m%d-%H%M%S}-{token_hex(2)}"
        week_label = week or "current"

        app_id = self._select_app_id()
        if app_id is None:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.NO_APP,
                "No Radarr connection is configured.", config,
            )

        try:
            resolved_week, entries = await self._fetch_chart(week)
        except WeekNotPublishedError:
            # Stored as nothing: a recorded failure would mark the week attempted, and
            # an attempted week is never offered as missing again. The caller decides
            # what to say; the next run simply tries once Mojo has published it.
            raise
        except ScrapeError as exc:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.SCRAPE_FAILED, str(exc), config
            )
        # Label the report with the week actually fetched (a picked/current week may have
        # stepped back over an in-progress one), so the report and its re-run are honest.
        week_label = resolved_week

        # Before touching Radarr: if this chart is what we already stored for the week,
        # there is nothing to record. Whoever fetched it first — the scheduler or the
        # admin — keeps the report, and the second attempt is a no-op rather than a
        # duplicate. Checked here so an unchanged week costs one page fetch and no
        # library read, lookup or poster work at all.
        unchanged = self._unchanged_report(week_label, entries, config)
        if unchanged is not None:
            self._audit.record(
                AuditAction.PIPELINE_RUN, trigger=trigger, week=week_label,
                movies=len(entries), unchanged=True,
            )
            return unchanged
        if self._shrank(week_label, entries, config):
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.SCRAPE_FAILED,
                f"Box Office Mojo listed only {len(entries)} title(s) for a week already stored "
                "with more. Kept the stored chart.",
                config,
            )

        try:
            radarr = self._make_radarr(app_id)
            library = await radarr.list_movies()
        except KeyError:
            # The connection was deleted between selecting it and building the client
            # (AppNotFoundError subclasses KeyError) — report it rather than crash.
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.NO_APP,
                "The Radarr connection was removed while the run was in progress.", config,
            )
        except RadarrError as exc:
            return self._save_failed(
                report_id, run_at, trigger, week_label, RunStatus.RADARR_FAILED,
                f"Could not read the Radarr library: {exc}", config,
            )

        library_by_tmdb = {movie.tmdb_id: movie for movie in library}

        # One read for the whole run, so every chart entry is judged against the same
        # list even if the admin ignores something while the run is in flight.
        ignored = self._ignore.snapshot()
        # Read once for the whole run, for the same reason the ignore list is: every row
        # is then judged against the same answers, rather than against whatever the file
        # held at the moment that row happened to be built.
        corrections = self._corrections.book()
        # One budget for the whole run, so five guessed titles cost five release pages and
        # a sixth costs none. Reached only on a chart that changed — the unchanged check
        # above returns before any of this.
        lookups = _ReleaseLookups(boxoffice.MAX_RELEASE_LOOKUPS_PER_RUN)
        # Together rather than one by one: each title waits on Radarr's metadata service,
        # so twenty in a row was twenty round trips end to end. Safe to overlap because
        # everything they share — the lookup budget above and the release-id cache — is
        # read and written with no await in between, and every run lives on the one
        # event loop. `gather` keeps the results in chart order.
        limit = asyncio.Semaphore(RECONCILE_CONCURRENCY)

        async def reconcile(entry: BoxOfficeEntry) -> MovieResult:
            async with limit:
                return await self._reconcile(
                    entry, radarr, library_by_tmdb, ignored, corrections, lookups
                )

        results = await asyncio.gather(*[reconcile(entry) for entry in entries])
        # No await from here to the write below, on purpose. fix_match also runs on this
        # event loop with none between its two writes, so one of these two blocks finishes
        # before the other starts, and an admin's choice cannot fall between them.
        results = self._with_corrections_made_meanwhile(
            results, corrections, library_by_tmdb, ignored
        )
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
            region=config.region,
            currency=_chart_currency(entries),
            totals=ReportTotals(movies=len(results), matched=matched),
            movies=results,
        )
        self._reports.replace_week(report)
        self._reports.prune(config.report_keep)
        self._audit.record(
            AuditAction.PIPELINE_RUN, trigger=trigger, week=week_label,
            movies=len(results), matched=matched,
        )
        return report

    def _with_corrections_made_meanwhile(
        self,
        results: list[MovieResult],
        seen: CorrectionBook,
        library_by_tmdb: dict[int, RadarrMovie],
        ignored: IgnoreSnapshot,
    ) -> list[MovieResult]:
        """Apply any correction that arrived after this run read the list.

        It has already been written to the stored weeks, so committing this run's rows as
        they stand would quietly overwrite an admin's decision with a guess.
        """
        current = self._corrections.book()
        updated: list[MovieResult] = []
        for result in results:
            identity = (result.normalized_title, result.release_path, result.source_imdb_id)
            correction = current.for_row(*identity)
            if correction is None or correction == seen.for_row(*identity):
                updated.append(result)
                continue
            common = {field: getattr(result, field) for field in _CHART_FIELDS}
            common["title"] = correction.title
            updated.append(
                self._result(
                    common, _links_from_correction(correction, []), library_by_tmdb, ignored
                )
            )
        return updated

    async def _reconcile(
        self,
        entry: BoxOfficeEntry,
        radarr: object,
        library_by_tmdb: dict[int, RadarrMovie],
        ignored: IgnoreSnapshot,
        corrections: CorrectionBook,
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
            "release_path": entry.release_path,
            # Whatever a run has already learned. Spending a page to learn it is decided
            # below, only where the answer would change something.
            "source_imdb_id": (
                self._release_ids.get(entry.release_path) if entry.release_path else None
            ),
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
        correction = self._correction_for(entry, common, corrections)
        if correction is not None and correction.release_path is None and entry.release_path:
            # A title-only correction, about to be applied to a row that can say which film
            # it is. Worth one page: this is how a remake escapes its namesake's correction.
            common["source_imdb_id"] = await self._source_imdb(entry, lookups)
            correction = self._correction_for(entry, common, corrections)
        if correction is not None:
            common["title"] = correction.title
            return self._result(
                common, _links_from_correction(correction, candidates_raw),
                library_by_tmdb, ignored,
            )

        candidates = [Candidate(item.title, item.year, item) for item in candidates_raw]
        title_match = find_match(entry.title, candidates)
        matched: RadarrLookupResult | None = title_match.payload if title_match else None
        known = common["source_imdb_id"]
        contradicted = bool(matched and known and matched.imdb_id and matched.imdb_id != known)
        ambiguous = bool(title_match and title_match.ambiguous)
        # A title that settled it costs nothing further. Otherwise, before falling back to
        # a suggestion — which is how a remake, a sequel or a re-release ends up on the wrong
        # card — ask Mojo which film this row actually is. An IMDb id is exact where a title
        # is not, and both sides already have one.
        confirmed = None
        if matched is None or ambiguous or contradicted:
            common["source_imdb_id"] = await self._source_imdb(entry, lookups)
            confirmed = await self._confirm_by_imdb(
                radarr, common["source_imdb_id"], candidates_raw
            )
        # Still nothing to show: Radarr's search found no such title, and no id found it.
        if confirmed is None and not candidates_raw:
            return MovieResult(**common, status=MovieStatus.MISSING, action=MovieAction.NO_MATCH)
        # Still a guess only when neither route identified it. That case keeps the amber
        # hint it has always had; the fix-match flow is what it is for.
        best: RadarrLookupResult = confirmed or matched or candidates_raw[0]
        detail = None
        if confirmed is None and (matched is None or contradicted):
            detail = MATCHED_BY_GUESS
        elif confirmed is None and ambiguous:
            detail = MATCHED_AMBIGUOUS
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
            "detail": detail,
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

    @staticmethod
    def _correction_for(
        entry: BoxOfficeEntry, common: dict, corrections: CorrectionBook
    ) -> Correction | None:
        return corrections.for_row(
            common["normalized_title"], entry.release_path, common["source_imdb_id"]
        )

    async def _source_imdb(self, entry: BoxOfficeEntry, lookups: _ReleaseLookups) -> str | None:
        """The IMDb id Mojo's release page names for this row, or None.

        None for every ordinary disappointment — the row linked no page, the run has spent
        its budget, the page could not be read. This exists to improve an answer, never to
        endanger a run.
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
        return imdb_id

    async def _confirm_by_imdb(
        self, radarr: object, imdb_id: str | None, candidates: list[RadarrLookupResult]
    ) -> RadarrLookupResult | None:
        """The film carrying that id: among what the search found, else by asking for it.

        Radarr's title search can miss a film entirely when Mojo spells it differently, which
        is exactly when an id is the only thing left to go on. A failure here leaves whatever
        the run already had, like every other step that exists only to improve a guess.
        """
        if imdb_id is None:
            return None
        found = next((item for item in candidates if item.imdb_id == imdb_id), None)
        if found is not None:
            return found
        try:
            exact = await radarr.lookup(RADARR_IMDB_TERM.format(imdb_id=imdb_id))
        except RadarrError:
            return None
        return next((item for item in exact if item.imdb_id == imdb_id), None)

    def _save_failed(
        self,
        report_id: str,
        run_at: datetime,
        trigger: str,
        week: str,
        status: str,
        error: str,
        config: _RunConfig,
    ) -> Report:
        report = Report(
            id=report_id,
            run_at=run_at.isoformat(),
            trigger=trigger,
            status=status,
            week=week,
            region=config.region,
            totals=ReportTotals(movies=0, matched=0),
            error=error,
        )
        if week != boxoffice.CURRENT_WEEK:
            self._reports.drop_failures_for_week(week)
        self._reports.save(report)
        self._reports.prune(config.report_keep)
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
