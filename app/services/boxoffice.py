"""Box Office Mojo weekly-chart scraper (Step 11, ruling #6).

`BoxMedia.md` itself flags scraping as "inherently fragile to upstream layout
changes", and the app runs silently — so parsing is defensive and fails LOUD: any
structural surprise raises `ScrapeError` and the raw HTML is snapshotted for
diagnosis, rather than silently yielding an empty chart that stops all
automation. Columns are located by header text, not fixed positions, so a
reordered column does not break matching.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError

# Deliberately duplicated with app.core.config.DEFAULT_BOXOFFICE_URL so core never
# imports services (avoids a circular import). Used as the fetch default and by tests.
BOM_WEEKLY_URL = "https://www.boxofficemojo.com/weekly/"
CURRENT_WEEK = "current"
# Both chart paths: /weekly/ is the domestic chart, /weekend/ is what Mojo publishes per
# region. Same week ids, same table headers, so only the path segment differs.
_WEEK_LINK_RE = re.compile(r"/week(?:ly|end)/(\d{4}W\d{2})/")
DOMESTIC_REGION = ""  # no ?area= — the /weekly/ chart, exactly as before this existed
# Mojo's `?area=` codes, by the name the Settings dropdown shows. Deliberately WITHOUT a
# currency column: the currency is read off the page (see `_currency_prefix`) rather than
# asserted here, because a table claiming "GB means £" would be a guess about what a page
# this app cannot see at build time actually prints — and a wrong guess would put the
# wrong symbol in front of real money. What Mojo shows is what gets stored.
#
# A wrong code fails LOUD rather than quietly: the fetched page carries no chart table,
# `_locate_chart` raises, and the raw HTML is snapshotted for the Maintenance card.
REGIONS: dict[str, str] = {
    DOMESTIC_REGION: "Domestic (US & Canada)",
    "GB": "United Kingdom",
    "DE": "Germany",
    "NL": "Netherlands",
    "FR": "France",
    "ES": "Spain",
    "IT": "Italy",
    "AU": "Australia",
    "NZ": "New Zealand",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "BR": "Brazil",
}
DEFAULT_CURRENCY_SYMBOL = "$"
# The symbol in front of an amount: one to three non-digit characters, and only when
# digits actually follow. "A$", "R$", "NZ$" and "€" all fit; a "-" in an empty cell does
# not, because nothing follows it.
_CURRENCY_RE = re.compile(r"^\s*([^\d\s,.]{1,3})\s*[\d]")


def validated_region(value: object) -> str:
    """A stored region code, or domestic for anything this build does not ship.

    Read-tolerant, write-strict — the `users._validated_theme` pattern. A hand-edited
    filters.yml naming a region that no longer exists loads as domestic rather than
    refusing to start; the Settings route refuses the same value outright.
    """
    return value if isinstance(value, str) and value in REGIONS else DOMESTIC_REGION


def _currency_prefix(text: str) -> str | None:
    """The currency symbol a gross cell carries, or None when it carries none.

    Read rather than assumed. Whether Mojo prints a regional chart in local currency or in
    dollars is a fact about a page, and the difference between "£81.5M" and "$81.5M" is
    not cosmetic — so this app repeats what the page said instead of deciding for it.
    """
    match = _CURRENCY_RE.match(text)
    return match.group(1) if match else None


def bom_week_id(day: date) -> str:
    """Box Office Mojo week identifier for a date, e.g. '2026W03' (ISO week)."""
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}W{iso_week:02d}"


def week_chart_url(
    base_url: str, week: str | None, *, area: str = DOMESTIC_REGION
) -> str:
    """Build the chart URL. `week` None -> the current chart; else that week's page.

    Domestic keeps `/weekly/` untouched. A region needs `/weekend/`, because that is the
    only chart Mojo publishes per area — so regional figures are weekend figures, which
    the Settings note says out loud. Only a trailing `weekly` segment is rewritten, so an
    operator who pointed `BM_BOXOFFICE_URL` somewhere else keeps whatever they configured
    and simply gains the query.
    """
    trimmed = base_url.rstrip("/")
    if area:
        if trimmed.endswith("/weekly"):
            trimmed = trimmed[: -len("weekly")] + "weekend"
        suffix = f"/{week}/" if week and week != CURRENT_WEEK else "/"
        return f"{trimmed}{suffix}?area={quote(area, safe='')}"
    if week and week != CURRENT_WEEK:
        return f"{trimmed}/{week}/"
    return f"{trimmed}/"


def find_latest_week(index_html: str) -> str | None:
    """The most recent week id linked on the /weekly/ index (e.g. '2026W31'), or None.

    The fixed 'YYYYWNN' format makes lexicographic max == chronological latest.
    """
    weeks = _WEEK_LINK_RE.findall(index_html)
    return max(weeks) if weeks else None


def week_start(week: str) -> date | None:
    """The Monday a 'YYYYWNN' week id begins on, or None when it isn't one.

    Public because callers outside this module need the same answer — which calendar
    month a week belongs to, and what date to print beside it. One definition, so a
    week can never begin on two different days depending on who asked.
    """
    match = re.fullmatch(r"(\d{4})W(\d{2})", week)
    if match is None:
        return None
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return None


def is_week_id(week: str) -> bool:
    """True for a week that actually exists, e.g. '2026W31'.

    Stricter than the 'YYYYWNN' shape on purpose: '2026W99' and '2026W00' match the shape
    but are not real ISO weeks, and fetching them would be a pointless request to the
    chart host. Shares `_week_monday` with the week arithmetic so this module stays the
    single owner of what a week id is.
    """
    return week_start(week) is not None


def previous_week_id(week: str) -> str | None:
    """The BOM week id one ISO week before `week` ('2026W32' -> '2026W31'), or None if
    `week` is not a 'YYYYWNN' id. isocalendar handles the year boundary."""
    monday = week_start(week)
    return bom_week_id(monday - timedelta(days=7)) if monday else None


def next_week_id(week: str) -> str | None:
    """The BOM week id one ISO week after `week` ('2026W31' -> '2026W32'), or None if
    `week` is not a 'YYYYWNN' id."""
    monday = week_start(week)
    return bom_week_id(monday + timedelta(days=7)) if monday else None


def week_chip_label(week: str, *, with_year: bool) -> str:
    """A week id as it reads on a chip or a trend line: '2026W02' -> "W02" or "W02 '26".

    The year is carried only when the list the label sits in spans more than one, because
    it is redundant in the usual case and the labels sit in narrow places — a poster card
    and a table cell. Once two Januaries are stored, "W02" alone names two different weeks
    and the chips beside it are no longer distinguishable.

    Anything that is not a week id is returned unchanged rather than sliced into
    nonsense: every caller builds these from stored report weeks today, and a label is not
    the place to discover that changed.
    """
    if week_start(week) is None:
        return week
    return f"W{week[5:]} \u2019{week[2:4]}" if with_year else f"W{week[5:]}"


def spans_multiple_years(weeks: list[str]) -> bool:
    """Whether a set of week ids needs its labels to carry the year."""
    return len({week[:4] for week in weeks}) > 1


USER_AGENT = "BoxMedia/0.1 (+self-hosted media automation; contact via your reverse proxy)"
REQUEST_TIMEOUT_SECONDS = 20.0
TOP_N = 10
# Operator-selectable depth, 1 to 30 — the range Box Office Mojo's own weekly chart
# offers. The ceiling is scrape politeness, not a parser limit: every extra title is
# another Radarr lookup and poster fetch per run. The floor is 1 because "only the
# chart-topper" is a real way to use this — a depth of one still records a week, and every
# feature built on the history (the month leaderboard, the trend lines, the dashboard's
# weeks-tracked) already slices defensively.
MIN_CHART_SIZE = 1
MAX_CHART_SIZE = 30
# Independent of the depth above, and deliberately: this asks whether the PAGE looks like
# a real chart, before any slice. A depth of 1 against a week Mojo has barely started
# reporting must still fail as an incomplete page rather than quietly record its one row.
MIN_EXPECTED_ROWS = 5  # a healthy weekly chart has ~10+; fewer signals breakage
# BOM lists the running week early with only a row or two; when resolving "current",
# step back over up to this many thin weeks to reach the last complete chart.
CURRENT_WEEK_MAX_LOOKBACK = 3

# How many release pages one run may fetch. Only a guessed title costs one, and only on a
# chart that actually changed (`_unchanged_report` returns first), so in a settled week
# this is zero — but a week where Radarr recognises nothing must still not turn into a
# dozen extra requests at Box Office Mojo.
MAX_RELEASE_LOOKUPS_PER_RUN = 5
# The film's IMDb id as a release page links it. Bounded, so a stray "/title/tt" in
# navigation markup does not read as an id.
#
# The trailing lookahead is what makes the bound safe rather than merely tidy: without it
# a longer id than this range allows would still MATCH, truncated — and a truncated id is
# not a near miss, it is a different film's id, which is exactly the wrong-poster failure
# this whole feature exists to stop. An id outside the range now yields nothing, and
# nothing falls back to the guess it was already making.
RELEASE_IMDB_RE = re.compile(r"/title/(tt\d{7,9})(?!\d)")
# The only href shape this app will follow. Anchored and alphanumeric-only, so an
# absolute URL, a protocol-relative one, a traversal or a scheme simply fails to match
# rather than being rejected by a check somebody could later reorder: what reaches
# `urljoin` can only ever be a path on Mojo's own host.
#
# Matched against the PATH, because Mojo's own chart links carry a `?ref_=` tracking
# parameter and a rule that rejected them would leave this feature never firing against
# the real site. The query is dropped rather than carried: it says which page linked
# here, which is neither ours to send back nor part of what identifies the film — and
# dropping it is also what keeps one film to one cache entry however it was linked.
_RELEASE_PATH_RE = re.compile(r"^/release/[A-Za-z0-9]+/$")

_REQUIRED_COLUMNS = ("rank", "release", "gross", "weeks")
# Read when present, never required — see BoxOfficeEntry.total_gross. A layout without any
# of these still yields a chart; only the four above are load-bearing.
TOTAL_GROSS_COLUMN = "total_gross"
THEATERS_COLUMN = "theaters"
GROSS_CHANGE_COLUMN = "gross_change_pct"
# Mojo's header for the week-over-week change, as `.strip().lower()` leaves it. The sign
# character is U+00B1 and there is no space before it.
_GROSS_CHANGE_HEADER = "%± lw"
# The shape of a percentage cell: an optional sign, then the whole part. Anchored on the
# first number in the cell so a stray prefix like "<" or ">" does not become a digit.
#
# The minus class carries the typographic forms as well as the ASCII one. The page is not
# pure ASCII — its own header for this column uses U+00B1 — and a real minus read as no
# sign at all turns a 38% collapse into a 38% climb, which is worse than reading nothing.
_MINUS_SIGNS = "-\u2212\u2013"  # hyphen-minus, minus sign, en dash
# re.escape on the class contents, because a bare hyphen between two other characters is
# a RANGE: `[+-\u2212]` spans + through U+2212 and therefore matches digits, which read
# "12%" as a sign of "1" and a magnitude of 2.
_PERCENT_RE = re.compile(
    rf"(?P<sign>[{re.escape('+' + _MINUS_SIGNS)}])?\s*(?P<whole>[\d,]+)(?:\.\d+)?\s*%"
)
_BILLION = 1_000_000_000


class ScrapeError(Exception):
    """The box-office chart could not be fetched or parsed."""


class BoxOfficeEntry(BaseModel):
    rank: int = Field(ge=1)
    title: str = Field(min_length=1)
    gross_amount: int = Field(ge=0)
    weeks_in_release: int = Field(ge=1)
    # Mojo's "Total Gross" — the film's running box-office total, not this week's take.
    # Optional: it is shown beside the weekly figure, never used to decide anything, so a
    # layout without the column must still yield a usable chart rather than no chart.
    total_gross: int | None = Field(default=None, ge=0)
    # How many screens it played on, and how the week's take moved against the last one.
    # Both optional for the same reason total_gross is: a layout without the column must
    # still yield a usable chart rather than no chart. Together they are the add-or-skip
    # signal a card could not show — a film shedding 38% on 4,071 screens is a different
    # proposition from one holding steady, and gross alone does not say which it is.
    # What this row's own gross cell printed in front of the number. Per row because that
    # is where it was read; every row of one chart carries the same one in practice, and
    # a row that carried none is honest about it rather than borrowing a neighbour's.
    currency_symbol: str | None = None
    theaters: int | None = Field(default=None, ge=0)
    # Signed, and deliberately not bounded: a re-release or an awards bump can post a
    # four-figure climb, and clamping it would misreport the one week worth noticing.
    gross_change_pct: int | None = None
    # Mojo's own page for this release, when the title cell linked one. Kept so a title
    # Radarr cannot recognise can be confirmed by IMDb id instead of guessed at — see
    # `fetch_release_imdb_id`. Routing rather than chart data, which is why it is
    # deliberately absent from the pipeline's fingerprint: a rotated id must not rewrite
    # a week whose figures never moved.
    release_path: str | None = None


def format_gross(amount: int, symbol: str = DEFAULT_CURRENCY_SYMBOL) -> str:
    """81_514_000 -> '$81.5M', 1_205_000_000 -> '$1.21B', for display.

    A week's take never approaches a billion, but a running total routinely passes it,
    and '$1205.0M' is both wider and harder to read than the figure it stands for.

    The symbol defaults to a dollar so every existing caller is unchanged; a regional
    chart passes whatever its own page printed.
    """
    millions = round(amount / 1_000_000, 1)
    # Tested after rounding, else 999,999,999 renders as "$1000.0M" — the one string
    # wider than anything the layout was measured against.
    if millions >= 1000:
        return f"{symbol}{amount / _BILLION:.2f}B"
    return f"{symbol}{millions:.1f}M"


def _clean_int(text: str) -> int | None:
    digits = "".join(character for character in text if character.isdigit())
    return int(digits) if digits else None


def _clean_percent(text: str) -> int | None:
    """'-38%' -> -38, '+12%' / '12%' -> 12, '-' -> None.

    A separate parser rather than `_clean_int`, which drops the sign — and the sign IS the
    fact here: a film at -38% and one at +38% are opposite stories. A cell with no digits
    at all is Mojo saying there was no previous week to compare against (every rank in its
    first week), which is not zero.
    """
    match = _PERCENT_RE.search(text)
    if match is None:
        return None
    # Truncated at the decimal point rather than rounded: this is shown as a whole
    # percent, and "0.6%" reading as 1% would be the card inventing a bigger move than
    # Mojo reported. Sweeping up every digit instead — the obvious shortcut — turns
    # "<0.1%" into 1% and "-0.5%" into -5%.
    magnitude = int(match.group("whole").replace(",", ""))
    sign = match.group("sign")
    return -magnitude if sign is not None and sign in _MINUS_SIGNS else magnitude


def _find_column_indices(header_cells: list[str]) -> dict[str, int] | None:
    indices: dict[str, int] = {}
    for position, label in enumerate(header_cells):
        normalized = label.strip().lower()
        if normalized == "rank" and "rank" not in indices:
            indices["rank"] = position
        elif normalized == "release" and "release" not in indices:
            indices["release"] = position
        elif normalized == "gross" and "gross" not in indices:  # this week's take
            indices["gross"] = position
        elif normalized == "total gross" and TOTAL_GROSS_COLUMN not in indices:
            indices[TOTAL_GROSS_COLUMN] = position
        elif normalized == "weeks" and "weeks" not in indices:
            indices["weeks"] = position
        elif normalized == THEATERS_COLUMN and THEATERS_COLUMN not in indices:
            indices[THEATERS_COLUMN] = position
        elif normalized == _GROSS_CHANGE_HEADER and GROSS_CHANGE_COLUMN not in indices:
            indices[GROSS_CHANGE_COLUMN] = position
    if all(column in indices for column in _REQUIRED_COLUMNS):
        return indices
    return None


def _locate_chart(soup: BeautifulSoup) -> tuple[list, dict[str, int]]:
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [cell.get_text(strip=True) for cell in rows[0].find_all(["th", "td"])]
        indices = _find_column_indices(header_cells)
        if indices is not None:
            return rows[1:], indices
    raise ScrapeError("box-office results table not found (page layout may have changed)")


def parse_chart(html: str, *, top_n: int = TOP_N) -> list[BoxOfficeEntry]:
    """Pure parse: HTML -> validated top-`top_n` entries. Raises ScrapeError on anomaly.

    A chart shorter than `top_n` yields everything it has; the completeness check below
    is against MIN_EXPECTED_ROWS, so asking for more never turns a valid week into an error.
    """
    soup = BeautifulSoup(html, "html.parser")
    data_rows, indices = _locate_chart(soup)

    entries: list[BoxOfficeEntry] = []
    # Bounded by the REQUIRED columns only: total gross is optional, and counting its
    # index here would reject rows this parser accepts today.
    highest_index = max(indices[column] for column in _REQUIRED_COLUMNS)
    total_index = indices.get(TOTAL_GROSS_COLUMN)
    theaters_index = indices.get(THEATERS_COLUMN)
    change_index = indices.get(GROSS_CHANGE_COLUMN)
    for row in data_rows:
        cells = row.find_all(["td", "th"])
        if len(cells) <= highest_index:
            continue
        rank = _clean_int(cells[indices["rank"]].get_text(strip=True))
        if rank is None:
            continue  # not a movie row (ad/section separator)
        gross_text = cells[indices["gross"]].get_text(strip=True)
        gross = _clean_int(gross_text)
        weeks = _clean_int(cells[indices["weeks"]].get_text(strip=True))
        release_cell = cells[indices["release"]]
        title = release_cell.get_text(strip=True)
        release_path = _release_path(release_cell)
        total_gross = None
        if total_index is not None and len(cells) > total_index:
            total_gross = _clean_int(cells[total_index].get_text(strip=True))
        theaters = None
        if theaters_index is not None and len(cells) > theaters_index:
            theaters = _clean_int(cells[theaters_index].get_text(strip=True))
        gross_change_pct = None
        if change_index is not None and len(cells) > change_index:
            gross_change_pct = _clean_percent(cells[change_index].get_text(strip=True))
        try:
            entries.append(
                BoxOfficeEntry(
                    rank=rank,
                    title=title,
                    gross_amount=gross if gross is not None else 0,
                    weeks_in_release=weeks if weeks is not None else 1,
                    total_gross=total_gross,
                    currency_symbol=_currency_prefix(gross_text),
                    theaters=theaters,
                    gross_change_pct=gross_change_pct,
                    release_path=release_path,
                )
            )
        except ValidationError:
            continue

    if len(entries) < MIN_EXPECTED_ROWS:
        # The table parsed cleanly with the right columns but has too few rows. That's
        # almost always an in-progress week BOM hasn't fully reported yet — NOT a layout
        # change (that surfaces as "results table not found" in _locate_chart). Say so
        # honestly so a re-run of the current week isn't blamed on the scraper.
        raise ScrapeError(
            f"only {len(entries)} title(s) listed for this week — it may not be fully "
            f"reported yet (a complete week lists {MIN_EXPECTED_ROWS}+). Use Run current "
            "week, or pick a completed past week."
        )
    entries.sort(key=lambda entry: entry.rank)
    return entries[:top_n]


async def fetch_weekly_chart(
    *,
    snapshot_dir: Path,
    url: str = BOM_WEEKLY_URL,
    week: str | None = None,
    top_n: int = TOP_N,
    area: str = DOMESTIC_REGION,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, list[BoxOfficeEntry]]:
    """Fetch and parse a chart, returning (resolved_week_id, entries).

    `week` None -> the current week, which steps back over the in-progress week BOM
    hasn't fully reported yet to the last complete chart. A specific `week` (a picked
    date / re-run) is fetched EXACTLY — no silent fallback — so a week with no data
    surfaces an honest "no data available" error instead of a different week. The
    returned week id is the one actually parsed, or "current" for the bare index page
    (the test mock). On failure, snapshot raw HTML and re-raise.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    html = ""
    try:
        candidates = await _week_candidates(client, url, week, area=area)
        for position, candidate in enumerate(candidates):
            html = await _get_text(client, week_chart_url(url, candidate, area=area))
            try:
                return (candidate or CURRENT_WEEK, parse_chart(html, top_n=top_n))
            except ScrapeError:
                if position < len(candidates) - 1:
                    continue  # current-week lookback: step back to the previous week
                if week and week != CURRENT_WEEK:
                    # A picked/re-run week is fetched exactly. If it has no chart (a future
                    # week, or one not yet reported), say so honestly rather than showing a
                    # different week or a misleading "layout changed".
                    raise ScrapeError(
                        f"No box-office data available for week {week} — it may be in the "
                        "future or not yet reported. Pick a completed past week, or use Run "
                        "current week."
                    ) from None
                raise  # current week exhausted its lookback -> surface the underlying error
        raise ScrapeError("no box-office week could be resolved")  # unreachable: guards len>=1
    except httpx.HTTPError as exc:
        raise ScrapeError(f"could not fetch box-office chart: {exc}") from exc
    except ScrapeError:
        _snapshot_failure(snapshot_dir, html)
        raise
    finally:
        if owns_client:
            await client.aclose()


def _release_path(cell: object) -> str | None:
    """The release page the title cell links to, or None when it links nothing usable.

    An allow-list rather than a rejection list, because the value ends up in a URL this
    app then fetches: only `/release/<alphanumeric>/` survives, so there is no spelling of
    an off-site host, a traversal or a query that could reach `urljoin` at all.
    """
    link = cell.find("a", href=True)
    if link is None:
        return None
    path = link["href"].strip().split("?", 1)[0].split("#", 1)[0]
    return path if _RELEASE_PATH_RE.match(path) else None


async def fetch_release_imdb_id(
    client: httpx.AsyncClient, base_url: str, release_path: str
) -> str | None:
    """The IMDb id a Mojo release page links, or None.

    None on every failure — an unreachable page, a redirect to something else, a layout
    without the link. This runs to make a guess better, so it must never be able to make
    a run worse: a title that cannot be confirmed simply stays the guess it already was.

    The path is re-checked here rather than trusted from the caller. It is validated at
    parse time too, but this is the function that builds a URL and fetches it, and a
    guard belongs where the request is made.
    """
    if not _RELEASE_PATH_RE.match(release_path):
        return None
    try:
        response = await client.get(urljoin(base_url, release_path))
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    match = RELEASE_IMDB_RE.search(response.text)
    return match.group(1) if match else None


async def _get_text(client: httpx.AsyncClient, target: str) -> str:
    response = await client.get(target)
    response.raise_for_status()
    return response.text


async def _week_candidates(
    client: httpx.AsyncClient, url: str, week: str | None, *, area: str = DOMESTIC_REGION
) -> list[str | None]:
    """Weeks to try. A specific `week` is fetched exactly — `[week]`, no fallback — so a
    picked week with no data errors honestly instead of resolving to a different week.
    Only the current week steps back over the in-progress week to the last complete chart.
    `None` means the bare page itself (the test mock serves a chart there)."""
    if week and week != CURRENT_WEEK:
        return [week]
    try:
        index = await client.get(week_chart_url(url, None, area=area))
        latest = find_latest_week(index.text)
    except httpx.HTTPError:
        latest = None
    if latest is None:
        return [None]  # bare page (test mock serves a chart there)
    candidates: list[str | None] = [latest]
    current = latest
    for _ in range(CURRENT_WEEK_MAX_LOOKBACK):
        previous = previous_week_id(current)
        if previous is None:
            break
        candidates.append(previous)
        current = previous
    return candidates


# Writing, listing, serving and clearing snapshots must all agree on the name, or the
# maintenance card silently stops matching what the scraper writes.
SNAPSHOT_PREFIX = "scrape-failure-"
SNAPSHOT_SUFFIX = ".html"
SNAPSHOT_DIGEST_LENGTH = 8
# Built from the three constants above rather than written out, so a change to any of them
# cannot leave the allow-list matching a shape nothing produces any more. The alphabet has
# no separator in it, which is what makes a traversal unrepresentable rather than merely
# rejected: `..%2Fx` and `../x` both simply fail to match.
_SNAPSHOT_NAME_RE = re.compile(
    rf"^{re.escape(SNAPSHOT_PREFIX)}\d+-[0-9a-f]{{{SNAPSHOT_DIGEST_LENGTH}}}"
    rf"{re.escape(SNAPSHOT_SUFFIX)}$"
)
# Naming by content instead of by length removes the accidental bound that overwriting
# used to provide, so the bound becomes explicit. An error page carrying a timestamp or
# request id hashes differently every run; this is what stops /data/logs growing on a
# scraper that stays broken for months.
MAX_SNAPSHOTS = 20


def list_snapshots(snapshot_dir: Path) -> list[tuple[str, int, float]]:
    """Every stored snapshot as (name, bytes, mtime), newest first.

    Bounded to this module's own filename pattern and non-recursive, exactly as
    `clear_snapshots` is: the card must list only what BoxMedia wrote, never whatever else
    happens to be in the log directory. Capped at MAX_SNAPSHOTS by the pruner, so this is
    at most twenty stat calls on a flat directory.

    Filtered through the SAME allow-list `snapshot_path` serves by, not just the glob, so
    listed and downloadable cannot disagree — a row that 404s on click would be worse than
    no row. A file from before the digest was part of the name is therefore not listed;
    `clear_snapshots` still sweeps it, so it cannot accumulate either.
    """
    if not snapshot_dir.is_dir():
        return []
    found: list[tuple[str, int, float]] = []
    for path in snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}"):
        if not _SNAPSHOT_NAME_RE.match(path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # pruned between the glob and the stat — it is simply not listed
        found.append((path.name, stat.st_size, stat.st_mtime))
    found.sort(key=lambda entry: entry[2], reverse=True)
    return found


def snapshot_path(snapshot_dir: Path, name: str) -> Path | None:
    """One snapshot by name, or None when the name is not one this module writes.

    An allow-list, the shape `posters.serve_path` uses: the request never contributes a
    path segment, only a name that has to match what `_snapshot_failure` produces.
    """
    if not _SNAPSHOT_NAME_RE.match(name):
        return None
    candidate = snapshot_dir / name
    return candidate if candidate.is_file() else None


def clear_snapshots(snapshot_dir: Path) -> int:
    """Delete the raw-HTML debugging snapshots. Returns the count removed.

    Bounded to this module's own filename pattern, non-recursively: the maintenance
    button must never reach anything BoxMedia didn't write.
    """
    if not snapshot_dir.is_dir():
        return 0
    removed = 0
    for path in snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}"):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _snapshot_failure(snapshot_dir: Path, html: str) -> None:
    if not html:
        return
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    # Named by content, still without a wall-clock dependency. Length alone collided:
    # two different failing pages of the same size overwrote each other, destroying the
    # evidence the snapshot exists to keep. Adding the digest separates them, while the
    # SAME failure recurring reuses its own name instead of piling up identical copies.
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:SNAPSHOT_DIGEST_LENGTH]
    name = f"{SNAPSHOT_PREFIX}{len(html)}-{digest}{SNAPSHOT_SUFFIX}"
    snapshot_dir.joinpath(name).write_text(html, encoding="utf-8")
    _prune_snapshots(snapshot_dir)


def _prune_snapshots(snapshot_dir: Path) -> None:
    """Keep only the newest MAX_SNAPSHOTS, newest by mtime."""
    snapshots = sorted(
        snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in snapshots[MAX_SNAPSHOTS:]:
        stale.unlink(missing_ok=True)
