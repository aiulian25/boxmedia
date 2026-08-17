"""Weekly reports list + detail, and the manual add/ignore actions.

The detail page ("weekly view") is where the admin reviews a week's chart and
decides what to add to Radarr — a run never adds anything itself. Each title shows
its live Radarr state so we never create a duplicate, and warn before fetching a
different quality of something already downloaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse

from app.core.audit import AuditAction
from app.services.boxoffice import (
    CURRENT_WEEK,
    bom_week_id,
    format_gross,
    is_week_id,
    next_week_id,
    previous_week_id,
    spans_multiple_years,
    week_chip_label,
    week_start,
)
from app.services.corrections import Correction
from app.services.filters import SCHEDULE_MODE_CADENCE
from app.services.ignore import IgnoreSnapshot
from app.services.matcher import normalize_title
from app.services.radarr import RadarrClient, RadarrError, RadarrLookupResult, RadarrMovie
from app.services.radarr_options import RadarrOptions
from app.services.reports import MATCHED_BY_GUESS, Report, RunStatus, RunTrigger, imdb_url
from app.web.deps import (
    BANNER_ERROR,
    DETAIL_STATUS_MESSAGES,
    cache_posters,
    client_ip,
    current_user,
    effective_defaults,
    format_timestamp,
    load_all_radarr_libraries,
    load_all_radarr_options,
    load_all_radarr_queues,
    load_radarr_library,
    load_radarr_options,
    optional_int,
    radarr_client_for,
    radarr_locations,
    render,
    target_views,
)
from app.web.movies import MOVIE_PATH

router = APIRouter()

NAV_KEY = "reports"
REPORTS_PATH = "/reports"
RUN_PATH = "/run"
DISPLAY_TIME_LENGTH = 16  # "YYYY-MM-DDTHH:MM"

BAD_WEEK_MESSAGE = (
    "That date didn’t parse — pick a week with the date field (YYYY-MM-DD)."
)
FIX_MATCH_PATH = "/fix-match"
SEARCH_PATH = "/reports/search"
BACKFILL_PATH = "/run-backfill"
MAX_MATCH_CANDIDATES = 5
# A one-letter query legitimately matches most of the history; the modal shows the
# strongest matches rather than rendering (and fetching posters for) all of them.
MAX_SEARCH_RESULTS = 25
# The query is echoed back in the modal heading. Bounded so a pathological one cannot
# stretch the dialog, the same reason display_name and the audit actor are bounded.
MAX_QUERY_LENGTH = 80
# Hard wall-time bound on the Radarr calls a user waits on. Deliberately longer than the
# 4s health probe: those are read-only pokes that can be abandoned freely, while an add or
# an upgrade is a WRITE — a timeout does not prove it did not happen, so the bound is set
# to catch a hung resolver rather than to abandon a slow but working request. Matches
# RadarrClient's own per-request default, so it only ever adds the coverage httpx lacks
# (name resolution), never shortens a request httpx would have allowed.
RADARR_ACTION_TIMEOUT_SECONDS = 15.0
TREND_WEEKS = 5  # how many recent weeks a trend line shows
# How many titles the month's leaderboard names. Five fits one row of the poster grid
# at the desktop breakpoint, which is what keeps it a summary rather than a second list.
MONTH_TOP_COUNT = 5
# Said instead of format_timestamp's "—" when no scheduled run has ever happened. The two
# are different facts: "—" reads as a time we do not know, while this one is a run that
# has not occurred — the state the line exists to make visible, after a scheduler that
# had silently never fired went unnoticed for weeks.
NEVER_RAN = "never"
# A closed value, never a URL: un-ignoring from the Settings list returns there instead
# of to a report, without letting the form choose an arbitrary redirect target.
SETTINGS_TARGET = "settings"


def _resolve_week(week: str, week_date: str) -> str | None:
    """A BOM week id (re-run) or a 'YYYY-MM-DD' date -> that week; empty -> None (current).

    Raises ValueError when a date was supplied but doesn't parse, so the caller can tell
    the user rather than silently fetching the current week."""
    week = week.strip()
    if week and week != CURRENT_WEEK:
        if not is_week_id(week):
            # Anything else is a crafted POST: the date picker and the Re-run button both
            # send this exact shape. Left unchecked it becomes the report's stored label
            # AND the path of the outbound chart request — `../../x` normalizes to a
            # different path on the chart host.
            raise ValueError(f"not a week id: {week!r}")
        return week
    value = week_date.strip()
    if value:
        return bom_week_id(date.fromisoformat(value))  # ValueError propagates on a bad date
    return None


def _redirect_reports(request: Request, status_code: str = "") -> RedirectResponse:
    url_base = request.app.state.settings.url_base
    suffix = f"?status={status_code}" if status_code else ""
    return RedirectResponse(
        url=f"{url_base}{REPORTS_PATH}{suffix}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _redirect_detail(request: Request, report_id: str, status_code: str = "") -> RedirectResponse:
    url_base = request.app.state.settings.url_base
    target = f"{REPORTS_PATH}/{report_id}" if report_id else REPORTS_PATH
    suffix = f"?status={status_code}" if status_code else ""
    return RedirectResponse(
        url=f"{url_base}{target}{suffix}", status_code=status.HTTP_303_SEE_OTHER
    )


def _after_add(
    request: Request, report_id: str, tmdb_id: int
) -> Callable[[str], RedirectResponse]:
    """Where an add reports back to — the week it came from, else the film's own page.

    The Add control now lives on the movie modal too, which opens from the dashboard and
    the month leaderboard and carries no report. Sending those to the reports list would
    land the user on a page that does not even render the outcome, so an add with no week
    returns to the film — where the result shows twice over: the banner, and the control
    replaced by where the film now lives.

    Both destinations are built from values the server already holds. There is no
    return-URL field for a crafted form to forge.
    """
    if report_id:
        return lambda status_code: _redirect_detail(request, report_id, status_code)
    url_base = request.app.state.settings.url_base
    return lambda status_code: RedirectResponse(
        url=f"{url_base}{MOVIE_PATH.format(tmdb_id=tmdb_id)}?status={status_code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _radarr_client(request: Request, app_id: str | None = None) -> RadarrClient:
    """A connection by id, defaulting to the primary. Callers guard that one exists."""
    return radarr_client_for(
        request,
        app_id or request.app.state.apps.primary_id(),
        timeout=RADARR_ACTION_TIMEOUT_SECONDS,
    )


def _resolve_target(request: Request, requested: str) -> str | None:
    """The connection an Add should go to, or None when the request names one we do not
    have.

    The browser chooses WHICH configured connection, never what it points at: an id that
    is not in apps.yml is refused rather than trusted, so no crafted form can aim an add
    at an arbitrary host. Empty means the primary, which is what every existing form
    posts.
    """
    requested = requested.strip()
    if not requested:
        return request.app.state.apps.primary_id()
    known = {app.id for app in request.app.state.apps.list_apps()}
    return requested if requested in known else None


def _display_time(run_at: str) -> str:
    return run_at[:DISPLAY_TIME_LENGTH].replace("T", " ")


def _schedule_view(
    request: Request, reports: list[Report] | None = None
) -> dict[str, object] | None:
    """When the unattended run last fired and when it fires next, or None when no job is
    scheduled.

    None under a bare TestClient, where the lifespan that starts the scheduler never ran.

    `reports` is passed through so the last-run answer costs no second read of the history
    directory — the caller has just loaded it.
    """
    scheduler = request.app.state.scheduler
    next_run = scheduler.next_run_at() if scheduler else None
    if next_run is None:
        return None
    last_run = scheduler.last_run_at(reports)
    return {
        "last_run": format_timestamp(last_run) if last_run else NEVER_RAN,
        "next_run": format_timestamp(next_run),
        # How often, in whichever terms this schedule actually runs on. "every 168h" is
        # simply untrue of a cadence, and a line describing the schedule is the last place
        # to leave a stale sentence.
        "rhythm": _rhythm(scheduler),
    }


def _rhythm(scheduler: object) -> str:
    """The schedule's own description of how often it fires."""
    if scheduler.schedule_mode == SCHEDULE_MODE_CADENCE:
        # The days as Mojo's week reads, not the cron string: "sun,mon,wed,fri" is a
        # configuration value and this is a sentence.
        return "Sun · Mon · Wed · Fri"
    return f"every {scheduler.interval_hours}h"


def _week_start_display(week: str) -> str | None:
    """The calendar date a BOM week begins, as day/month/year (day-first, never
    American), or None if `week` isn't a 'YYYYWNN' id (e.g. 'current').

    Which date that is comes from `boxoffice.week_start` rather than a second copy of the
    ISO-calendar arithmetic — this used to re-derive it, so the two could disagree.
    """
    start = week_start(week)
    if start is None:
        return None
    return f"{start.day}/{start.month}/{start.year}"


def _month_top_grossers(reports: list[Report]) -> dict[str, object] | None:
    """The highest-grossing titles of the most recent month that has any data.

    The month is NAMED rather than called "this month": the current calendar month often
    has no fetched weeks yet — Box Office Mojo lags, and weeks are fetched deliberately —
    so a section promising "this month" would routinely be an empty box. Naming it makes
    the heading a fact instead of a claim, and it becomes the current month by itself as
    soon as a week from it is fetched.

    Ranked on summed WEEKLY gross: Mojo's "Gross" column is that week's take, so adding a
    title's weeks together gives the month's takings. Its "Total Gross" — the running
    cumulative — is carried alongside for display only; summing THAT would count the same
    money once per week.
    """
    by_month: dict[tuple[int, int], list[Report]] = {}
    seen_weeks: set[str] = set()
    for report in reports:  # newest first
        if report.status != RunStatus.OK or report.week == CURRENT_WEEK:
            continue
        if report.week in seen_weeks:
            continue  # a re-run of a week must not count its grosses twice
        start = week_start(report.week)
        if start is None:
            continue
        seen_weeks.add(report.week)
        by_month.setdefault((start.year, start.month), []).append(report)
    if not by_month:
        return None

    # The newest month with ANY data. Before the first week of a new month is fetched
    # that is still last month, so the section is never empty; the moment one lands it
    # becomes the new month, thin at first and filling out with each further week.
    year, month = max(by_month)
    month_reports = by_month[(year, month)]
    # One currency for the whole board, taken from the newest week in the month. A month
    # can hold weeks fetched from different charts — the region is a setting, and a week
    # keeps what it was fetched with — and adding pounds to dollars would produce a
    # ranking that is wrong rather than merely mislabelled. The weeks left out are
    # counted and said, because a leaderboard quietly built from half a month is worse
    # than one that admits it.
    wanted_currency = month_reports[0].currency
    excluded_weeks = 0
    totals: dict[str, dict[str, object]] = {}
    for report in month_reports:
        if report.currency != wanted_currency:
            excluded_weeks += 1
            continue
        for movie in report.movies:
            # Reports arrive newest-first, so the first sighting is the freshest record
            # and supplies the poster and ids; later ones only add their gross.
            entry = totals.setdefault(
                movie.normalized_title,
                {
                    "title": movie.title,
                    "tmdb_id": movie.tmdb_id,
                    "poster_url": movie.poster_url,
                    "total_gross": None,
                    "gross_amount": 0,
                    "weeks": 0,
                },
            )
            entry["gross_amount"] += movie.gross_amount
            entry["weeks"] += 1
            # A running total only ever grows, so the largest seen is the current one.
            # Taking it this way rather than from the newest report keeps the figure right
            # when a re-run puts an older week at the front of the list.
            if movie.total_gross is not None:
                entry["total_gross"] = max(entry["total_gross"] or 0, movie.total_gross)

    top = sorted(totals.values(), key=lambda entry: entry["gross_amount"], reverse=True)
    top = top[:MONTH_TOP_COUNT]
    for position, entry in enumerate(top, start=1):
        entry["rank"] = position
        # Ranked on the month's takings; the running total is shown beside it so a film's
        # overall scale is visible without the monthly figure looking wrong.
        entry["gross_display"] = format_gross(entry["gross_amount"], wanted_currency)
        entry["total_gross_display"] = (
            format_gross(entry["total_gross"], wanted_currency)
            if entry["total_gross"]
            else None
        )
    return {
        "label": f"{date(year, month, 1):%B %Y}",
        "entries": top,
        # The weeks this board is actually built from, not every week the month holds.
        "weeks_tracked": len(month_reports) - excluded_weeks,
        "excluded_weeks": excluded_weeks,
        "titles": len(totals),
    }


def _by_week(reports: list[Report]) -> list[Report]:
    """The grid, newest week first.

    One report per week means this list IS the weeks, so it should read like them.
    Ordering by when a report was written instead would send a refreshed old week to the
    front of the grid the moment its figures were revised. `run_at` breaks ties, which is
    what a failed attempt at a week already holding data sorts on.
    """
    return sorted(reports, key=lambda report: (report.week, report.run_at), reverse=True)


def _card(report: Report) -> dict:
    return {
        "id": report.id,
        "movies": report.totals.movies,
        "matched": report.totals.matched,
        "week_date": _week_start_display(report.week),
        "trigger": report.trigger,
        "week": report.week,
        "status": report.status,
        "ok": report.status == RunStatus.OK,
    }


def _missing_view(weeks: list[str]) -> dict[str, object] | None:
    """The holes, labelled, or None when the history has none.

    Labelled here rather than sliced in the template, the same reason the trend line is:
    a run of gaps that crosses New Year reads "W52 \u201925" and "W01 \u201926" rather than
    claiming a jump from week 52 to week 1.
    """
    if not weeks:
        return None
    with_year = spans_multiple_years(weeks)
    return {
        "count": len(weeks),
        "first": week_chip_label(weeks[0], with_year=with_year),
        "last": week_chip_label(weeks[-1], with_year=with_year),
        "labels": [week_chip_label(week, with_year=with_year) for week in weeks],
    }


@router.post(BACKFILL_PATH)
async def run_backfill(request: Request) -> RedirectResponse:
    """Fetch every gap in stored history, oldest first, one at a time.

    The request carries NOTHING: the weeks are read from stored history inside this
    handler, so no submitted value can steer what gets fetched or how many. It is a POST
    rather than a GET because it starts work and is therefore CSRF-guarded like every
    other mutation, and it returns immediately — the cards are the progress.
    """
    user = current_user(request)
    weeks = request.app.state.reports.missing_weeks()
    if request.app.state.backfill.start(weeks):
        request.app.state.audit.record(
            AuditAction.PIPELINE_RUN, actor=user.username, source_ip=client_ip(request),
            trigger="backfill", weeks=len(weeks),
        )
    # No banner for a refusal. The page it returns to already says "Backfilling week X —
    # N of M", which is the same news said better; a second click is almost always the
    # same click, and answering it with a warning would be answering a question nobody
    # asked.
    return _redirect_reports(request)


@router.get(REPORTS_PATH)
async def reports_list(request: Request) -> object:
    current_user(request)
    reports = request.app.state.reports.list_reports()
    latest = reports[0] if reports else None
    last_run_failed = latest is not None and latest.status != RunStatus.OK
    bad_week = request.query_params.get("status") == "bad_week"
    schedule = _schedule_view(request, reports)
    # Computed from the report list already in hand, so the leaderboard costs no extra
    # read; only its five posters are fetched, and the weekly view has usually cached them.
    month_top = _month_top_grossers(reports)
    if month_top is not None:
        await cache_posters(request, month_top["entries"])
    return render(
        request,
        "reports.html",
        active_nav=NAV_KEY,
        month_top=month_top,
        cards=[_card(report) for report in _by_week(reports)],
        # Both from the list already in hand — the gaps cost no extra read, and the
        # runner's status is in memory.
        missing=_missing_view(request.app.state.reports.missing_weeks(reports)),
        backfill=request.app.state.backfill.status(),
        schedule=schedule,
        last_run_error=latest.error if last_run_failed else None,
        banner_kind=BANNER_ERROR if bad_week else None,
        banner_text=BAD_WEEK_MESSAGE if bad_week else None,
        search_path=SEARCH_PATH,
        backfill_path=BACKFILL_PATH,
        max_query_length=MAX_QUERY_LENGTH,
        query="",
    )


def _names(entries: list[dict[str, object]]) -> str:
    return " · ".join(str(entry["name"]) for entry in entries)


def _upgrade_target(holders_with_file: list[dict[str, object]]) -> dict[str, object] | None:
    """The connection a change-quality action should go to, or None when there isn't one.

    Only ever a box OTHER than the primary: the primary's own copy is handled by the
    fields above, read from the library this page already loaded. A downloaded copy is
    the requirement — changing the quality of a film still on its way means nothing, and
    a Radarr that reported no internal id for it gives the form nothing to name.

    The first holder wins when several have a file. Picking between two downloaded copies
    is a choice this card has no room to offer and no basis to make; the chips beside it
    say where the others are, and each one now opens that Radarr.
    """
    for entry in holders_with_file:
        if entry.get("radarr_id") is not None:
            return entry
    return None


def _movie_view(
    movie: object,
    library: dict[int, RadarrMovie] | None,
    ignored: IgnoreSnapshot,
    history: list[tuple[str, int, int]] | None = None,
    holders: list[dict[str, object]] | None = None,
) -> dict:
    tmdb = movie.tmdb_id
    radarr_movie = library.get(tmdb) if (library is not None and tmdb is not None) else None

    if library is None:
        # Radarr unreachable — fall back to the status recorded in the report.
        in_radarr = movie.status in ("in_library", "wanted")
        has_file = movie.status == "in_library"
        file_quality, radarr_id, quality_profile_id = None, None, None
    else:
        in_radarr = radarr_movie is not None
        has_file = bool(radarr_movie and radarr_movie.has_file)
        file_quality = radarr_movie.file_quality if radarr_movie else None
        radarr_id = radarr_movie.radarr_id if radarr_movie else None
        quality_profile_id = radarr_movie.quality_profile_id if radarr_movie else None

    # Somewhere else has it, even though the primary does not — say where instead of
    # calling it Missing, and distinguish a downloaded copy from one still on its way.
    elsewhere = [] if in_radarr else (holders or [])
    elsewhere_with_file = [entry for entry in elsewhere if entry["has_file"]]
    upgrade_target = _upgrade_target(elsewhere_with_file)
    # Read from `holders`, not `elsewhere`: a film the PRIMARY is downloading empties
    # `elsewhere` but is exactly the case this line exists for. The badge is deliberately
    # untouched — it is a fixed overlay sharing its row with the rank chip, and the percent
    # pushed it to 198px and two lines over that chip when measured.
    downloading = [
        entry for entry in (holders or []) if entry.get("progress") is not None
    ]

    is_ignored = ignored.is_ignored(tmdb, movie.normalized_title)
    if is_ignored and not in_radarr and not elsewhere:
        badge_label, badge_dot = "Ignored", "ignored"
    elif elsewhere_with_file:
        badge_label = f"In Library · {_names(elsewhere_with_file)}"
        badge_dot = "in_library"
    elif elsewhere:
        badge_label = f"Wanted · {_names(elsewhere)}"
        badge_dot = "wanted"
    elif in_radarr and has_file:
        badge_label = f"In Library · {file_quality}" if file_quality else "In Library"
        badge_dot = "in_library"
    elif in_radarr:
        badge_label, badge_dot = "Wanted", "wanted"
    elif movie.action == "no_match":
        badge_label, badge_dot = "No match", "missing"
    else:
        badge_label, badge_dot = "Missing", "missing"

    return {
        "rank": movie.rank,
        "title": movie.title,
        "normalized_title": movie.normalized_title,
        "gross_display": movie.gross_display,
        "weeks_in_release": movie.weeks_in_release,
        "tmdb_id": tmdb,
        "year": movie.year,
        "poster_url": movie.poster_url,
        "imdb_url": movie.imdb_url,
        "wiki_url": movie.wiki_url,
        "rating": movie.rating,
        "genres": movie.genres,
        "theaters": movie.theaters,
        "gross_change_pct": movie.gross_change_pct,
        "guessed": movie.detail == MATCHED_BY_GUESS,
        "in_radarr": in_radarr,
        "has_file": has_file,
        "file_quality": file_quality,
        "radarr_id": radarr_id,
        "quality_profile_id": quality_profile_id,
        "ignored": is_ignored,
        "elsewhere": elsewhere,
        "upgrade_target": upgrade_target,
        "downloading": downloading,
        # The last few weeks this title charted — the climb-or-collapse story. Labelled
        # here rather than sliced in the template, so a run that crosses New Year reads
        # "W52 \u201925 -> W01 \u201926" instead of claiming a film went from week 52 to week 1.
        "trend": _trend_view((history or [])[-TREND_WEEKS:]),
        "badge_label": badge_label,
        "badge_dot": badge_dot,
    }


def _trend_view(history: list[tuple[str, int, int, str]]) -> list[dict[str, object]]:
    """The trend line's weeks and ranks, each labelled for the run it sits in."""
    with_year = spans_multiple_years([week for week, _, _, _ in history])
    return [
        {"label": week_chip_label(week, with_year=with_year), "rank": rank}
        for week, rank, _, _ in history
    ]


def _week_neighbours(week: str, reports: list[Report]) -> dict[str, str | None]:
    """The weeks either side of this report, and the reports holding them if any.

    A neighbour that already has a report becomes a link; one that doesn't becomes a
    fetch. The forward side stops at the current ISO week — there is no point offering to
    fetch a week that hasn't happened.
    """
    previous_week = previous_week_id(week)
    next_week = next_week_id(week)
    if next_week and next_week > bom_week_id(date.today()):
        next_week = None

    existing = {report.week: report.id for report in reports}
    return {
        "previous_week": previous_week,
        "previous_link": existing.get(previous_week) if previous_week else None,
        "next_week": next_week,
        "next_link": existing.get(next_week) if next_week else None,
    }


# Registered BEFORE /reports/{report_id}: routes match in definition order, so with
# the parameterised one first this path is read as a report id called "search".
def _search_matches(reports: list[Report], query: str) -> list[dict[str, object]]:
    """Stored titles matching `query`, newest first, each with the weeks it charted.

    Matched on the normalized title both sides, so "spider man" finds "Spider-Man" — the
    same folding of punctuation, numerals and articles the pipeline already uses to
    recognise one film across two spellings.

    Reports arrive newest-first, so the first sighting of a title is its freshest record
    and supplies the poster and ids; later sightings only contribute their week. A re-run
    of a week is skipped exactly as `histories()` skips it, so a week is never listed
    twice.
    """
    wanted = normalize_title(query)
    if not wanted:
        return []
    seen_weeks: set[str] = set()
    by_title: dict[str, dict[str, object]] = {}
    for report in reports:
        if report.status != RunStatus.OK or report.week == CURRENT_WEEK:
            continue
        if report.week in seen_weeks:
            continue
        seen_weeks.add(report.week)
        for movie in report.movies:
            if wanted not in movie.normalized_title:
                continue
            match = by_title.setdefault(
                movie.normalized_title,
                {
                    "title": movie.title,
                    "year": movie.year,
                    "tmdb_id": movie.tmdb_id,
                    "poster_url": movie.poster_url,
                    "weeks": [],
                },
            )
            match["weeks"].append(
                {"week": report.week, "rank": movie.rank, "report_id": report.id}
            )
    for match in by_title.values():
        # By WEEK, not by when the report was written: re-running an old week today makes
        # its report the newest, and its chip would otherwise jump to the front of a run
        # it actually ends.
        match["weeks"].sort(key=lambda entry: entry["week"], reverse=True)
        _label_weeks(match["weeks"])
    return list(by_title.values())[:MAX_SEARCH_RESULTS]


def _label_weeks(weeks: list[dict[str, object]]) -> None:
    """Give each week entry the label its own list calls for.

    Decided per list rather than globally: a title that charted only in one year reads
    better without the year, even while another title on the same page carries it.
    """
    with_year = spans_multiple_years([str(entry["week"]) for entry in weeks])
    for entry in weeks:
        entry["label"] = week_chip_label(str(entry["week"]), with_year=with_year)


@router.get(SEARCH_PATH)
async def search_reports(request: Request, q: str = "", fragment: int = 0) -> object:
    """Find a title across every stored week, so a known film does not mean re-reading
    every report to locate it.

    `?fragment=1` returns the inner block for the modal; without it the same URL renders
    as a page, so the feature works with JavaScript off — the arrangement
    `/movies/{tmdb_id}` already uses.
    """
    current_user(request)
    query = q.strip()[:MAX_QUERY_LENGTH]
    reports = request.app.state.reports.list_reports()
    matches = _search_matches(reports, query)

    # The same three reads the weekly view makes, for the same reasons: which connections
    # already hold each title, and what the Add menu should offer.
    libraries, options_by_app, queues = await asyncio.gather(
        load_all_radarr_libraries(request),
        load_all_radarr_options(request),
        load_all_radarr_queues(request),
    )
    apps_by_id = {app.id: app for app in request.app.state.apps.list_apps()}
    for match in matches:
        match["locations"] = radarr_locations(
            match["tmdb_id"], libraries, apps_by_id, queues
        )
    await cache_posters(request, matches)
    targets = target_views(request, options_by_app)

    return render(
        request,
        "_search_results.html" if fragment else "search_results.html",
        active_nav=NAV_KEY,
        query=query,
        matches=matches,
        weeks_searched=len({report.week for report in reports if report.week != CURRENT_WEEK}),
        targets=targets,
        show_targets=len(targets) > 1,
    )


@router.get("/reports/{report_id}")
async def report_detail(request: Request, report_id: str) -> object:
    current_user(request)
    try:
        report = request.app.state.reports.get(report_id)
    except (FileNotFoundError, ValueError):
        return _redirect_reports(request)

    # Every connection's library and options at once — the target menu needs each
    # instance's own quality, and the badges need to know who already holds the film.
    libraries, options_by_app, queues = await asyncio.gather(
        load_all_radarr_libraries(request),
        load_all_radarr_options(request),
        load_all_radarr_queues(request),
    )
    primary_id = request.app.state.apps.primary_id()
    library = libraries.get(primary_id) if primary_id else None
    options = options_by_app.get(primary_id) or RadarrOptions()
    apps_by_id = {app.id: app for app in request.app.state.apps.list_apps()}
    targets = target_views(request, options_by_app)
    # One read of the history directory feeds both the trend lines and the week nav.
    all_reports = request.app.state.reports.list_reports()
    histories = request.app.state.reports.histories(all_reports)
    # One read for the whole grid, so every card is judged against the same list.
    ignored = request.app.state.ignore.snapshot()
    movies = [
        _movie_view(
            movie,
            library,
            ignored,
            histories.get(movie.normalized_title),
            radarr_locations(movie.tmdb_id, libraries, apps_by_id, queues),
        )
        for movie in report.movies
    ]
    await cache_posters(request, movies)

    filters = request.app.state.filters.load()
    # Only the upgrade form still asks for a quality, and only to preselect what the film
    # already has; adds take the connection's own profile without asking.
    primary_app = request.app.state.apps.get(primary_id) if primary_id else None
    default_profile_id = (
        effective_defaults(primary_app, options, filters)[0]
        if primary_app is not None
        else None
    )
    banner = DETAIL_STATUS_MESSAGES.get(request.query_params.get("status", ""))
    neighbours = _week_neighbours(report.week, all_reports)

    return render(
        request,
        "report_detail.html",
        active_nav=NAV_KEY,
        report=report,
        movies=movies,
        profiles=options.profiles,
        # Every connection's own list, so the change-quality select on a film held
        # elsewhere offers that box's profiles. Ids are per database: offering the
        # primary's list for the 4K box is exactly the bug the options cache was
        # re-keyed by connection to prevent.
        profiles_by_app={app_id: opts.profiles for app_id, opts in options_by_app.items()},
        default_profile_id=default_profile_id,
        targets=targets,
        # The split button only earns its caret when there is somewhere else to send to.
        show_targets=len(targets) > 1,
        previous_week=neighbours["previous_week"],
        previous_link=neighbours["previous_link"],
        next_week=neighbours["next_week"],
        next_link=neighbours["next_link"],
        display_time=_display_time(report.run_at),
        ok=report.status == RunStatus.OK,
        banner_kind=banner[0] if banner else None,
        banner_text=banner[1] if banner else None,
    )


@router.post(RUN_PATH)
async def run_now(
    request: Request,
    week: str = Form(""),
    week_date: str = Form(""),
) -> RedirectResponse:
    """Fetch a chart and produce a report — the current week, a picked date, or a re-run.

    Re-running refreshes a week when Box Office Mojo has changed it, and is a no-op when
    it has not; either way it reports, it never adds.
    """
    current_user(request)
    try:
        resolved = _resolve_week(week, week_date)
    except ValueError:
        return _redirect_reports(request, "bad_week")
    # Which reports existed before the run. Getting one of them back is precisely what
    # "nothing had changed" means, and it saves threading an outcome type through the
    # scheduler and every caller of pipeline.run for the sake of one message.
    known = {stored.id for stored in request.app.state.reports.list_reports()}
    scheduler = request.app.state.scheduler
    if resolved is None and scheduler is not None:
        report = await scheduler.run_now()
    else:
        report = await request.app.state.pipeline.run(trigger=RunTrigger.MANUAL, week=resolved)
    # Land on the report itself so the week's chart is what the user sees — the Box
    # Office dashboard is the library view and looks the same whatever week you run.
    return _redirect_detail(request, report.id, "unchanged" if report.id in known else "")


async def _lookup_candidates(request: Request, term: str) -> list[RadarrLookupResult]:
    """Ask the primary Radarr what films match a search term.

    Bounded like every other Radarr call the user waits on: a powered-off box whose name
    no longer resolves would otherwise hold the request for the OS resolver's timeout.
    """
    results = await asyncio.wait_for(
        _radarr_client(request).lookup(term), timeout=RADARR_ACTION_TIMEOUT_SECONDS
    )
    return results[:MAX_MATCH_CANDIDATES]


@router.get("/reports/{report_id}/fix-match")
async def fix_match_form(
    request: Request, report_id: str, rank: int = 0, term: str = ""
) -> object:
    """Search Radarr for the right film when a chart title matched nothing (or the wrong
    thing). Read-only: picking a result is a separate POST."""
    current_user(request)
    if not request.app.state.apps.primary_id():
        return _redirect_detail(request, report_id, "add_config")
    try:
        candidates = await _lookup_candidates(request, term)
    except (RadarrError, TimeoutError):
        return _redirect_detail(request, report_id, "add_failed")

    # The artwork and the score the lookup already returned. This page exists because two
    # films share a title, and a poster settles that faster than any amount of prose.
    candidate_views = [
        {
            "tmdb_id": candidate.tmdb_id,
            "title": candidate.title,
            "year": candidate.year,
            "overview": candidate.overview,
            "poster_url": candidate.poster_url,
            "rating": candidate.rating,
        }
        for candidate in candidates
    ]
    # Through the local cache, like every other poster in the app: the CSP is
    # img-src 'self', so a remote URL would simply not render. At most five candidates,
    # fetched concurrently, and the weekly view has usually cached them already.
    await cache_posters(request, candidate_views)

    return render(
        request,
        "fix_match.html",
        active_nav=NAV_KEY,
        report_id=report_id,
        rank=rank,
        term=term,
        candidates=candidate_views,
    )


@router.post(FIX_MATCH_PATH)
async def fix_match(
    request: Request,
    report_id: str = Form(...),
    rank: str = Form(...),
    term: str = Form(...),
    tmdb_id: str = Form(...),
) -> RedirectResponse:
    """Re-point a chart entry at the chosen film.

    Only the chosen tmdb id comes from the form: the poster URL and IMDb link are taken
    from Radarr's own answer, re-fetched here, so nothing the browser sends can put an
    arbitrary URL into a stored report (the poster is later fetched and rendered).

    The correction is remembered rather than applied to this row alone. The same film
    charts for weeks at a time, so a fix that reached only the week being looked at left
    the others wrong — and the next automatic check overwrote even that one.
    """
    user = current_user(request)
    chosen_rank = optional_int(rank)
    chosen_tmdb = optional_int(tmdb_id)
    if chosen_rank is None or chosen_tmdb is None:
        return _redirect_detail(request, report_id, "add_config")

    try:
        candidates = await _lookup_candidates(request, term)
    except (RadarrError, KeyError, TimeoutError):
        return _redirect_detail(request, report_id, "add_failed")

    match = next((item for item in candidates if item.tmdb_id == chosen_tmdb), None)
    if match is None:
        return _redirect_detail(request, report_id, "add_failed")

    correction = Correction(
        tmdb_id=match.tmdb_id,
        title=match.title,
        year=match.year,
        imdb_url=imdb_url(match.imdb_id),
        poster_url=match.poster_url,
    )
    try:
        # Filed under the CHART title, which is what a future run will have in hand — and
        # read from the stored row rather than the form, so nothing the browser sends can
        # file a correction against a different title than the one being fixed.
        charted = request.app.state.reports.normalized_title_at(report_id, chosen_rank)
        request.app.state.corrections.save(charted, correction)
        weeks = request.app.state.reports.apply_correction(charted, correction)
    except (FileNotFoundError, ValueError, KeyError):
        return _redirect_reports(request)

    request.app.state.audit.record(
        AuditAction.MATCH_CORRECTED,
        actor=user.username, source_ip=client_ip(request),
        report_id=report_id, rank=chosen_rank, tmdb_id=match.tmdb_id,
        charted_title=charted, weeks=weeks,
    )
    return _redirect_detail(request, report_id, "match_fixed")


@router.post("/add-movie")
async def add_movie(
    request: Request,
    report_id: str = Form(""),
    tmdb_id: str = Form(...),
    title: str = Form(...),
    year: str = Form(""),
    target: str = Form(""),
) -> RedirectResponse:
    """Add one title to a chosen Radarr connection — never a duplicate.

    `target` names WHICH configured connection and is the only thing the browser gets to
    choose; an unknown id is refused rather than trusted. The quality and folder are that
    connection's own, set once in Settings, so no crafted form can name a profile from
    another instance's database or point an add at an arbitrary library path.

    Where it returns to is derived here, never taken from the request: a report id when
    the add came from a week, otherwise the film's own page. Nothing carries a return URL,
    so no crafted form can bounce anyone off this app.
    """
    user = current_user(request)
    tmdb = optional_int(tmdb_id)
    app_id = _resolve_target(request, target)
    if app_id is None or tmdb is None:
        # No usable tmdb id means no film page to return to either, so this one outcome
        # keeps the old destination.
        return _redirect_detail(request, report_id, "add_config")
    back = _after_add(request, report_id, tmdb)

    app = request.app.state.apps.get(app_id)
    options = await load_radarr_options(request, app_id)
    filters = request.app.state.filters.load()
    profile_id, _, target_folder = effective_defaults(app, options, filters)
    if profile_id is None or not target_folder:
        return back("add_config")

    library = await load_radarr_library(request, app_id)
    if library is not None and tmdb in library:
        return back("already_in_radarr")

    try:
        await asyncio.wait_for(
            _radarr_client(request, app_id).add_movie(
                tmdb_id=tmdb, title=title, year=optional_int(year),
                quality_profile_id=profile_id, root_folder_path=target_folder,
            ),
            timeout=RADARR_ACTION_TIMEOUT_SECONDS,
        )
    except (RadarrError, TimeoutError):
        # A timeout cannot prove the add did not land, so this reports failure rather
        # than success. Re-adding is safe: the duplicate check above refuses a second
        # copy, and Radarr rejects one of its own accord.
        return back("add_failed")
    request.app.state.audit.record(
        AuditAction.MOVIE_ADDED_MANUAL, actor=user.username,
        tmdb_id=tmdb, quality_profile_id=profile_id, target=app.name,
    )
    return back("added")


@router.post("/upgrade-movie")
async def upgrade_movie(
    request: Request,
    report_id: str = Form(""),
    radarr_id: str = Form(...),
    quality_profile_id: str = Form(...),
    target: str = Form(""),
) -> RedirectResponse:
    """Fetch a different quality of a title already in the library (explicit confirm).

    `target` names WHICH configured connection holds the copy, exactly as it does for an
    add: an id that is not in apps.yml is refused rather than trusted, and empty means the
    primary — which is what the primary-held form has always posted. It matters because a
    Radarr id is meaningless on a box that did not issue it: sending one instance's id to
    another would upgrade a DIFFERENT film, or nothing at all.

    The profile is checked against that connection's own list before it is sent, for the
    same reason — profile ids are per database. Vetted only against a NON-EMPTY list, the
    `_validated_defaults` rule: an id we cannot check is not an id we know to be wrong, and
    refusing it would make an unreachable-Radarr fallback a dead end.
    """
    user = current_user(request)
    rid = optional_int(radarr_id)
    profile_id = optional_int(quality_profile_id)
    app_id = _resolve_target(request, target)
    if app_id is None or rid is None or profile_id is None:
        return _redirect_detail(request, report_id, "add_config")

    options = await load_radarr_options(request, app_id)
    if options.profiles and all(profile.id != profile_id for profile in options.profiles):
        return _redirect_detail(request, report_id, "add_config")

    app = request.app.state.apps.get(app_id)
    try:
        await asyncio.wait_for(
            _radarr_client(request, app_id).upgrade_movie(rid, profile_id),
            timeout=RADARR_ACTION_TIMEOUT_SECONDS,
        )
    except (RadarrError, TimeoutError):
        return _redirect_detail(request, report_id, "add_failed")
    request.app.state.audit.record(
        AuditAction.MOVIE_ADDED_MANUAL, actor=user.username,
        radarr_id=rid, quality_profile_id=profile_id, action_kind="upgrade",
        target=app.name,
    )
    return _redirect_detail(request, report_id, "upgraded")


@router.post("/ignore")
def ignore_movie(
    request: Request,
    report_id: str = Form(""),
    tmdb_id: str = Form(""),
    title: str = Form(...),
    normalized_title: str = Form(...),
) -> RedirectResponse:
    current_user(request)
    request.app.state.ignore.add(
        tmdb_id=optional_int(tmdb_id), title=title, normalized_title=normalized_title
    )
    return _redirect_detail(request, report_id, "ignored")


@router.post("/unignore")
def unignore_movie(
    request: Request,
    report_id: str = Form(""),
    tmdb_id: str = Form(""),
    normalized_title: str = Form(...),
    next_page: str = Form("", alias="next"),
) -> RedirectResponse:
    """Un-ignore a title from a weekly report, or from the list in Settings."""
    current_user(request)
    request.app.state.ignore.remove(
        tmdb_id=optional_int(tmdb_id), normalized_title=normalized_title
    )
    if next_page == SETTINGS_TARGET:
        url_base = request.app.state.settings.url_base
        return RedirectResponse(
            url=f"{url_base}/settings?status=unignored", status_code=status.HTTP_303_SEE_OTHER
        )
    return _redirect_detail(request, report_id, "unignored")


@router.post("/reports/{report_id}/delete")
def delete_report(request: Request, report_id: str) -> RedirectResponse:
    user = current_user(request)
    try:
        deleted = request.app.state.reports.delete(report_id)
    except ValueError:
        # An id that could never name a stored report — a crafted POST, not a user.
        # Same landing page as any other delete; nothing happened, so nothing is logged.
        return _redirect_reports(request)
    if deleted:
        request.app.state.audit.record(
            AuditAction.REPORT_DELETED,
            actor=user.username, source_ip=client_ip(request), report_id=report_id,
        )
    return _redirect_reports(request)
