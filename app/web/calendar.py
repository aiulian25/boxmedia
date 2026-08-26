"""The calendar — one week, both media (TV step 14).

The visible half of step 13. Every row on this page comes off the merged cache that
`app.services.calendar` writes; the page's only outbound work is the bounded refresh it
runs when that cache has gone stale, and even that only rebuilds a local file.

## Why a GET may write

The refresh is safe as a GET because it mutates nothing outside this install: it re-reads
the user's own Sonarr and Radarr and rewrites one cache file. Nothing is added, monitored
or deleted anywhere. It is bounded by `refresh_calendar`'s own wall and by the cache TTL,
so opening the page repeatedly costs one fetch a quarter of an hour, not one per view —
and a connection that just failed is skipped by the same backoff every other page uses.

## Anchored on a week, not on a day

The grid shows a Monday-to-Sunday week; the list underneath shows the next fortnight from
**today** whichever week is on screen, because "what is coming" does not change when you
look back at last week. The nav reaches exactly as far as the cache does — two weeks
either side — since a week the cache cannot answer would render as an empty grid, which
reads as "nothing is on" rather than "we never fetched that far".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Request

from app.services.calendar import (
    KIND_MOVIE,
    KIND_SERIES,
    STATE_DOWNLOADED,
    STATE_DOWNLOADING,
    STATE_MISSING,
    STATE_MONITORED,
    STATE_TODAY,
    WINDOW_DAYS,
    CalendarEntry,
    entries_for_day,
)
from app.web.deps import (
    MEDIA_TYPES,
    TYPE_MOVIES,
    TYPE_TV,
    current_user,
    format_timestamp,
    refresh_calendar,
    render,
    validated_media_type,
)

router = APIRouter()

NAV_KEY = "calendar"
CALENDAR_PATH = "/calendar"
WEEK_QUERY_KEY = "week"
DAYS_IN_WEEK = 7
# How far the nav travels, in weeks either side of this one — exactly the span the cache
# covers, so every week the nav offers is a week the page can actually answer.
WEEKS_EITHER_SIDE = WINDOW_DAYS // DAYS_IN_WEEK
# How much of the fortnight list is shown. The cache reaches further; this is the part a
# person can act on, and a list twice as long is one nobody reads to the end of.
LOOK_AHEAD_DAYS = 14

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TODAY_SUFFIX = "Today"

# What each state says on the card. `missing` is the internal name; amber here means
# "aired and not grabbed", never "lost" — the register this app uses everywhere else.
STATE_LABELS = {
    STATE_DOWNLOADED: "Downloaded",
    STATE_TODAY: "Airs today",
    STATE_MISSING: "Aired — no file yet",
    STATE_MONITORED: "Monitored",
}
# Said with the figure, so it is built rather than looked up.
DOWNLOADING_LABEL = "↓ {percent:.0f}%"

KIND_LABELS = {KIND_MOVIE: "Film", KIND_SERIES: "TV"}
# Which cached kind each chip keeps. `all` keeps everything and so is absent by design.
CHIP_KINDS = {TYPE_MOVIES: KIND_MOVIE, TYPE_TV: KIND_SERIES}

INCOMPLETE_NOTICE = (
    "Some connections did not answer — their entries below are the last ones fetched."
)
# Said instead of `format_timestamp`'s "—", for the reason the weekly page says it: the
# two are different facts. "—" reads as a time we do not know; this is a fetch that has
# not happened, which is the state the line exists to make visible.
NEVER_FETCHED = "never"


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _reachable_weeks(today: date) -> tuple[date, date]:
    """The earliest and latest Monday the cache can answer for."""
    this_week = _monday(today)
    span = timedelta(weeks=WEEKS_EITHER_SIDE)
    return this_week - span, this_week + span


def _resolve_week(raw: str | None, today: date) -> date:
    """The Monday of the week to show, clamped into what the cache covers.

    Read-tolerant, like every other query parameter here: a hand-edited or bookmarked
    `?week=` that is unparseable shows this week, and one that points years away shows the
    nearest week there is data for rather than an empty grid pretending nothing is on.
    """
    earliest, latest = _reachable_weeks(today)
    if not raw:
        return _monday(today)
    try:
        asked = date.fromisoformat(raw)
    except ValueError:
        return _monday(today)
    return min(max(_monday(asked), earliest), latest)


def _date_display(day: date) -> str:
    """Day-first, never American, and unpadded — the form `format_timestamp` already uses
    everywhere else in the app."""
    return f"{day.day}/{day.month}"


def _state_text(entry: CalendarEntry) -> str:
    if entry.state == STATE_DOWNLOADING and entry.progress is not None:
        return DOWNLOADING_LABEL.format(percent=entry.progress)
    return STATE_LABELS.get(entry.state, STATE_LABELS[STATE_MONITORED])


def _entry_view(entry: CalendarEntry, *, today: date) -> dict[str, object]:
    """One row, ready to place. The template decides where it goes, never what it says."""
    return {
        "kind": entry.kind,
        "kind_label": KIND_LABELS.get(entry.kind, KIND_LABELS[KIND_SERIES]),
        "title": entry.title,
        "sub": entry.sub,
        "state": entry.state,
        "state_text": _state_text(entry),
        # The connection is a tooltip rather than a column: with two Sonarrs it answers
        # "which box", and a long name must not be able to widen a day.
        "connection": entry.connection,
        "when": _when_display(entry.when, today=today),
    }


def _when_display(moment: datetime, *, today: date) -> str:
    """`Today 23:00` or `Thu 27/8 20:30` — the fortnight list's first column."""
    at = moment.astimezone(UTC)
    time_of_day = f"{at:%H:%M}"
    if at.date() == today:
        return f"{TODAY_SUFFIX} {time_of_day}"
    return f"{DAY_NAMES[at.weekday()]} {_date_display(at.date())} {time_of_day}"


def _kept(entries: list[CalendarEntry], media_type: str) -> list[CalendarEntry]:
    """The chip applied. All keeps everything, which is why it has no kind to match."""
    kind = CHIP_KINDS.get(media_type)
    if kind is None:
        return entries
    return [entry for entry in entries if entry.kind == kind]


def _week_view(
    entries: list[CalendarEntry], *, week_start: date, today: date
) -> list[dict[str, object]]:
    """Seven columns, Monday first, whether or not anything is on them."""
    columns = []
    for offset in range(DAYS_IN_WEEK):
        day = week_start + timedelta(days=offset)
        columns.append({
            "name": DAY_NAMES[day.weekday()],
            "date": _date_display(day),
            "today": day == today,
            "entries": [
                _entry_view(entry, today=today)
                for entry in entries_for_day(entries, day)
            ],
        })
    return columns


def _look_ahead(
    entries: list[CalendarEntry], *, today: date
) -> list[dict[str, object]]:
    """The next fortnight from today, anchored on now rather than on the week on screen.

    What is coming does not change when you look back at last week, and this is the part
    of the page where a long episode title actually has room to be read.
    """
    horizon = today + timedelta(days=LOOK_AHEAD_DAYS)
    return [
        _entry_view(entry, today=today)
        for entry in entries
        if today <= entry.day <= horizon
    ]


@router.get(CALENDAR_PATH)
async def calendar(
    request: Request, week: str = "", type: str = ""  # noqa: A002
) -> object:
    """The page. Renders from the cache, refreshing it first only when it has gone stale."""
    current_user(request)
    media_type = validated_media_type(type)
    today = datetime.now(UTC).date()
    week_start = _resolve_week(week, today)

    cache = request.app.state.calendar_cache
    complete = True
    if cache.is_stale():
        complete = await refresh_calendar(request)
    entries = _kept(cache.load(), media_type)

    earliest, latest = _reachable_weeks(today)
    previous_week = week_start - timedelta(weeks=1)
    next_week = week_start + timedelta(weeks=1)
    return render(
        request,
        "calendar.html",
        active_nav=NAV_KEY,
        calendar_path=CALENDAR_PATH,
        week_key=WEEK_QUERY_KEY,
        media_type=media_type,
        media_types=MEDIA_TYPES,
        days=_week_view(entries, week_start=week_start, today=today),
        look_ahead=_look_ahead(entries, today=today),
        look_ahead_days=LOOK_AHEAD_DAYS,
        # A week outside the cache is offered as nothing at all rather than as a link to
        # an empty grid — see the module docstring.
        previous_week=previous_week.isoformat() if previous_week >= earliest else None,
        previous_label=_date_display(previous_week),
        next_week=next_week.isoformat() if next_week <= latest else None,
        next_label=_date_display(next_week),
        this_week=week_start == _monday(today),
        # Carried by the chips so switching Movies/TV keeps the week you are looking at.
        week_value=week_start.isoformat(),
        last_fetch=_last_fetch_display(cache),
        # None until a scheduler is running — under a bare TestClient, or on an install
        # whose lifespan has not started one. The line then states the refresh that
        # actually happens instead of promising a time nothing keeps, which is the weekly
        # page's own rule: a schedule that has never fired says so.
        next_fetch=_next_fetch_display(request),
        incomplete_notice=None if complete else INCOMPLETE_NOTICE,
        configured=bool(request.app.state.apps.list_apps(kind=None)),
    )


def _next_fetch_display(request: Request) -> str | None:
    """When the morning job next re-reads the week, or None when nothing is scheduled."""
    scheduler = request.app.state.scheduler
    next_run = scheduler.next_calendar_run_at() if scheduler else None
    return format_timestamp(next_run) if next_run else None


def _last_fetch_display(cache: object) -> str:
    """When the cache was last written, or the plain fact that it never has been."""
    stamp = cache.fetched_at()
    if stamp is None:
        return NEVER_FETCHED
    return format_timestamp(datetime.fromtimestamp(stamp, UTC))
