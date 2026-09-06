"""Async Sonarr v3 REST client (TV step 3) — the television half of the app.

Deliberately the same shape as `radarr.py`: one `_request` that maps transport and
status failures to typed errors, `_json`/`_json_list` that refuse a surprising body,
and every outbound call validating TLS by default. `build_verify` is imported from
there rather than reimplemented, so the CA-file escape hatch behaves identically for
both services and there is one place to fix it.

The two clients are NOT collapsed into a shared base. Their error types name their
own app, which is what makes a banner readable, and Radarr's client is load-bearing
for every existing movie flow — refactoring it to serve television is a risk taken
for tidiness rather than for the user. If a third *arr ever arrives, that is the
moment to extract the base, with three call sites to prove the shape.

Where Sonarr genuinely differs from Radarr, the difference is the interesting part:

* **Series are keyed on TVDB ids**, not TMDB. Everything that adds a series has to
  arrive holding one; see `lookup`, which prefers the `tvdb:` term for that reason.
* **The queue is episode-level.** `queue()` folds it to one figure per SERIES, since
  that is what a card shows.
* **Seasons exist**, so a library entry carries per-season counts rather than a single
  `hasFile`.

On errors: this client never reads meaning out of an error body. nzb360's teardown
(`tv-discovery.md` §11) found duplicate-add detection resting on the literal strings
`SeriesExistsValidator` and "already been added", so any Sonarr rewording silently
degraded it to an unknown failure. The guard against a duplicate add is the library
snapshot, checked before posting. What the body IS used for is showing Sonarr's own
words to the person who pressed the button — extracted, bounded and stripped of
control characters, never matched against.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.core.values import int_or_none
from app.services.radarr import build_verify

API_PREFIX = "/api/v3"
REQUEST_TIMEOUT_SECONDS = 15.0
API_KEY_HEADER = "X-Api-Key"
# Sonarr names itself in /system/status. Radarr and Lidarr answer the same shape, so
# without checking this an "it works" would be a lie about which app answered — the
# same trap settings.py already guards for Radarr.
APP_NAME = "sonarr"
# One page of the queue. It decorates a grid, and a queue longer than the page is a
# queue whose tail nobody is looking at. Matches radarr.QUEUE_PAGE_SIZE.
QUEUE_PAGE_SIZE = 200

# How much of a series Sonarr should monitor on add. Sonarr's own vocabulary, and the
# four that answer a real question when adding: everything, only what has yet to air,
# a taster, or nothing yet.
MONITOR_ALL = "all"
MONITOR_FUTURE = "future"
MONITOR_FIRST_SEASON = "firstSeason"
MONITOR_NONE = "none"
MONITOR_OPTIONS = (MONITOR_ALL, MONITOR_FUTURE, MONITOR_FIRST_SEASON, MONITOR_NONE)

# Sonarr's three series types, passed through verbatim. Defined here rather than in
# apps.py because they are Sonarr's vocabulary, not the store's — the owning module
# holds the constant and everyone else imports it.
SERIES_TYPE_STANDARD = "standard"
SERIES_TYPES = (SERIES_TYPE_STANDARD, "daily", "anime")

# An error body is remote-supplied text on its way to a banner and an audit line. It is
# shown, never interpreted: bounded so a pathological body cannot fill a log line, and
# stripped of control characters so it cannot forge one. Same posture as the version
# string settings.py sanitises before rendering.
MAX_ERROR_DETAIL_CHARS = 200


class SonarrError(Exception):
    """Base class for all Sonarr client failures.

    `detail` carries Sonarr's own words when it sent any, so a refused add can say why
    in the words of the server that refused it. It is display text only — nothing in
    this app branches on its contents.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class SonarrAuthError(SonarrError):
    """Sonarr rejected the API key (HTTP 401/403)."""


class SonarrConnectionError(SonarrError):
    """Could not reach Sonarr, or its TLS certificate failed validation."""


@dataclass(frozen=True)
class SonarrSeason:
    """One season's shelf state. `episode_count` is what has aired, which is why it and
    `episode_file_count` together answer "am I missing anything yet"."""

    season_number: int
    monitored: bool
    episode_count: int
    episode_file_count: int
    size_on_disk: int

    @property
    def complete(self) -> bool:
        return self.episode_count > 0 and self.episode_file_count >= self.episode_count


@dataclass(frozen=True)
class SonarrSeries:
    """One entry in the Sonarr library.

    `tvdb_id` is the identity that matters: it is what Sonarr keys on, what a Trakt row
    carries for free, and what a TMDB row has to be bridged into before it can be added.
    """

    sonarr_id: int
    tvdb_id: int
    title: str
    year: int | None
    monitored: bool
    ended: bool
    episode_count: int
    episode_file_count: int
    imdb_id: str | None = None
    tmdb_id: int | None = None
    path: str | None = None
    title_slug: str | None = None
    poster_url: str | None = None
    quality_profile_id: int | None = None
    seasons: tuple[SonarrSeason, ...] = ()

    @property
    def missing_episode_count(self) -> int:
        """Aired episodes with no file. Never negative: Sonarr counts specials in some
        totals and not others, and a card showing "-2 missing" would be nonsense."""
        return max(0, self.episode_count - self.episode_file_count)

    @property
    def complete(self) -> bool:
        return self.episode_count > 0 and self.missing_episode_count == 0


@dataclass(frozen=True)
class SonarrLookupResult:
    tvdb_id: int
    title: str
    year: int | None
    overview: str | None
    poster_url: str | None
    status: str | None
    network: str | None
    imdb_id: str | None = None
    tmdb_id: int | None = None


@dataclass(frozen=True)
class SonarrCalendarEpisode:
    """One episode on the calendar, with enough of its series to render a row without a
    second call — which is why `calendar` asks Sonarr to include the series."""

    episode_id: int
    series_id: int
    series_title: str
    tvdb_id: int | None
    season_number: int
    episode_number: int
    title: str | None
    air_date_utc: datetime | None
    has_file: bool
    monitored: bool


def _image_url(images: object, cover_type: str) -> str | None:
    """Sonarr's images array, same shape as Radarr's. `remoteUrl` first: `url` is a path
    on the Sonarr host, which is not reachable from a browser pointed at BoxMedia."""
    if not isinstance(images, list):
        return None
    for image in images:
        if isinstance(image, dict) and image.get("coverType") == cover_type:
            return image.get("remoteUrl") or image.get("url")
    return None


def _season(item: object) -> SonarrSeason | None:
    if not isinstance(item, dict):
        return None
    number = int_or_none(item.get("seasonNumber"))
    if number is None:
        return None
    statistics = item.get("statistics")
    stats = statistics if isinstance(statistics, dict) else {}
    return SonarrSeason(
        season_number=number,
        monitored=bool(item.get("monitored", False)),
        episode_count=int_or_none(stats.get("episodeCount")) or 0,
        episode_file_count=int_or_none(stats.get("episodeFileCount")) or 0,
        size_on_disk=int_or_none(stats.get("sizeOnDisk")) or 0,
    )


def _ended(item: dict) -> bool:
    """Whether the show has finished airing.

    `ended` is the boolean and `status` is the word Sonarr shows; prefer the boolean and
    fall back, so a build that drops one still answers. Written out rather than as
    `item.get("ended", <fallback>)` because that only reaches the fallback when the key
    is ABSENT — a payload carrying `"ended": null` would take None as the answer and read
    as still-running.
    """
    ended = item.get("ended")
    if isinstance(ended, bool):
        return ended
    return item.get("status") == "ended"


def _library_series(item: dict) -> SonarrSeries:
    """One `/series` entry -> SonarrSeries. Shared by the full list and the add response
    so the two can never drift in what they read — `radarr._library_movie`'s reason."""
    statistics = item.get("statistics")
    stats = statistics if isinstance(statistics, dict) else {}
    raw_seasons = item.get("seasons")
    seasons = [_season(entry) for entry in raw_seasons] if isinstance(raw_seasons, list) else []
    return SonarrSeries(
        sonarr_id=int_or_none(item.get("id")) or 0,
        tvdb_id=int_or_none(item.get("tvdbId")) or 0,
        title=item.get("title") or "",
        year=int_or_none(item.get("year")),
        monitored=bool(item.get("monitored", False)),
        ended=_ended(item),
        episode_count=int_or_none(stats.get("episodeCount")) or 0,
        episode_file_count=int_or_none(stats.get("episodeFileCount")) or 0,
        imdb_id=item.get("imdbId") or None,
        tmdb_id=int_or_none(item.get("tmdbId")),
        path=item.get("path") or None,
        title_slug=item.get("titleSlug") or None,
        poster_url=_image_url(item.get("images"), "poster"),
        quality_profile_id=int_or_none(item.get("qualityProfileId")),
        seasons=tuple(season for season in seasons if season is not None),
    )


def _air_date(value: object) -> datetime | None:
    """Sonarr's `airDateUtc`, e.g. "2026-08-26T21:00:00Z".

    Unparseable or absent yields None rather than raising: one malformed episode must
    not take down a whole week of calendar.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_detail(response: httpx.Response) -> str | None:
    """Sonarr's own words for a refusal, for showing — never for matching.

    Sonarr v3 answers a rejected POST with either a list of validation objects or a
    single object; both carry the human sentence. Bounded and stripped of control
    characters before it goes anywhere near a log line or a page.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return None
    for field in ("errorMessage", "message", "error"):
        text = payload.get(field)
        if isinstance(text, str) and text.strip():
            clean = "".join(char for char in text if char.isprintable()).strip()
            return clean[:MAX_ERROR_DETAIL_CHARS] or None
    return None


class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify: bool | str = True,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify = verify
        self._timeout = timeout

    def _httpx_verify(self) -> bool | ssl.SSLContext:
        # A CA-file path is turned into an SSLContext (httpx deprecates str paths).
        if isinstance(self._verify, str):
            return ssl.create_default_context(cafile=self._verify)
        return self._verify

    def _client(self) -> httpx.AsyncClient:
        # The key rides a header, never a query string: a URL reaches proxy logs and
        # browser history, and this one is the credential to a download pipeline.
        return httpx.AsyncClient(
            base_url=f"{self._base_url}{API_PREFIX}",
            headers={API_KEY_HEADER: self._api_key},
            timeout=self._timeout,
            verify=self._httpx_verify(),
        )

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except (httpx.ConnectError, OSError) as exc:
            # OSError covers ssl.SSLError and a misconfigured CA path (a directory or a
            # missing file) raised while building the SSL context — degrade to
            # "unreachable" rather than 500-ing every page that touches Sonarr.
            raise SonarrConnectionError(f"cannot reach Sonarr at {self._base_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SonarrConnectionError(f"request to Sonarr failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise SonarrAuthError("Sonarr rejected the API key")
        if response.status_code >= 400:
            raise SonarrError(
                f"Sonarr returned HTTP {response.status_code}",
                detail=_error_detail(response),
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> object:
        """Parse a Sonarr response body, mapping a non-JSON 200 to SonarrError.

        A proxy in front of Sonarr can answer any path with an HTML login or error
        page; every caller catches SonarrError, while JSONDecodeError escapes to a
        500 (web) or an unrecorded crash (scheduler).
        """
        try:
            return response.json()
        except ValueError as exc:
            raise SonarrError("Sonarr returned a non-JSON response") from exc

    @staticmethod
    def _json_list(response: httpx.Response) -> list:
        """Same, for the endpoints whose answer is iterated straight away.

        A bare `for item in ...` over a dict silently yields its KEYS, so a shape
        surprise would become wrong data rather than a caught error.
        """
        items = SonarrClient._json(response)
        if not isinstance(items, list):
            raise SonarrError("unexpected Sonarr response shape")
        return items

    async def system_status(self) -> dict:
        """Used by the Settings 'Test Connection' button to prove reachability + key.

        The caller checks `appName` against APP_NAME: Radarr answers this path with the
        same shape, so a Radarr address pasted into a Sonarr card would otherwise pass.
        """
        response = await self._request("GET", "/system/status")
        status = self._json(response)
        if not isinstance(status, dict):
            raise SonarrError("unexpected Sonarr status response shape")
        return status

    async def list_series(self) -> list[SonarrSeries]:
        response = await self._request("GET", "/series")
        return [
            _library_series(item)
            for item in self._json_list(response)
            if isinstance(item, dict)
        ]

    async def series_by_tvdb(self, tvdb_id: int) -> SonarrSeries | None:
        """One library entry by TVDB id, or None when Sonarr does not have it.

        None means "Sonarr answered, and does not have it". A failure to reach Sonarr
        raises, so the caller can tell "not in the library" from "could not look" — the
        distinction the add flow needs before it decides to post.
        """
        response = await self._request("GET", "/series", params={"tvdbId": tvdb_id})
        matches = self._json(response)
        if not isinstance(matches, list) or not matches:
            return None
        first = matches[0]
        if not isinstance(first, dict):
            raise SonarrError("unexpected Sonarr response shape")
        # Older Sonarr builds ignore an unrecognised filter and return the whole library,
        # so a mismatched answer is treated as "not found" rather than as this series.
        series = _library_series(first)
        return series if series.tvdb_id == tvdb_id else None

    async def queue(self) -> dict[int, float]:
        """How far along each downloading SERIES is, as a percentage, keyed by Sonarr's
        own series id.

        Sonarr queues EPISODES, but a card shows a series — so several episode records
        fold into one figure. The lowest wins, for the reason Radarr's queue takes the
        lowest across a film's parts: a series is no nearer than its slowest episode, and
        a card claiming 98% while an episode sits at 10% would be a lie about when it is
        watchable.

        Defensive in the same way the list endpoints are: a proxy can answer this path
        with anything, and a record Sonarr has not sized yet is 0%, not a division by zero.
        """
        response = await self._request("GET", "/queue", params={"pageSize": QUEUE_PAGE_SIZE})
        payload = self._json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise SonarrError("unexpected Sonarr queue response shape")
        progress: dict[int, float] = {}
        for record in payload["records"]:
            if not isinstance(record, dict):
                continue
            series_id = int_or_none(record.get("seriesId"))
            size, left = record.get("size"), record.get("sizeleft")
            if series_id is None:
                continue
            if not isinstance(size, int | float) or not isinstance(left, int | float):
                continue
            if isinstance(size, bool) or isinstance(left, bool):
                continue
            # max(0, ...) because Sonarr reports sizeleft > size briefly while it revises
            # an estimate, and a negative percentage would render as a negative fill.
            percent = 0.0 if size <= 0 else max(0.0, min(100.0, 100 * (1 - left / size)))
            progress[series_id] = min(progress.get(series_id, percent), percent)
        return progress

    async def lookup(self, term: str) -> list[SonarrLookupResult]:
        """Search Sonarr's own metadata. Pass `tvdb:{id}` for an exact match.

        Sonarr accepts both a plain title and a `tvdb:` term; the id form is what the add
        flow uses, because a title search can return the wrong show and there is no year
        in a chart row to disambiguate with.
        """
        response = await self._request("GET", "/series/lookup", params={"term": term})
        results = []
        for item in self._json_list(response):
            if not isinstance(item, dict):
                continue
            results.append(
                SonarrLookupResult(
                    tvdb_id=int_or_none(item.get("tvdbId")) or 0,
                    title=item.get("title") or "",
                    year=int_or_none(item.get("year")),
                    overview=item.get("overview") or None,
                    poster_url=_image_url(item.get("images"), "poster"),
                    status=item.get("status") or None,
                    network=item.get("network") or None,
                    imdb_id=item.get("imdbId") or None,
                    tmdb_id=int_or_none(item.get("tmdbId")),
                )
            )
        return results

    async def lookup_by_tvdb(self, tvdb_id: int) -> SonarrLookupResult | None:
        """The exact series behind a TVDB id, or None when Sonarr's metadata has none.

        None is the honest answer for a show TMDB gave us a TVDB id for that Sonarr
        cannot resolve — the add flow refuses rather than posting a guess.
        """
        for result in await self.lookup(f"tvdb:{tvdb_id}"):
            if result.tvdb_id == tvdb_id:
                return result
        return None

    async def add_series(
        self,
        *,
        tvdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitor: str = MONITOR_ALL,
        season_folder: bool = True,
        series_type: str = SERIES_TYPE_STANDARD,
        search_on_add: bool = True,
        monitored: bool = True,
    ) -> SonarrSeries:
        """Add one series to Sonarr.

        `monitor` is the only choice a person makes per show — quality and folder come
        from the connection, set once in Settings, exactly as the film add works.

        The series itself stays monitored even when `monitor` is "none": that is Sonarr's
        own behaviour, and it is what lets someone start monitoring a season later without
        re-adding. Nothing here checks for a duplicate; the caller does that against the
        library snapshot before it gets this far.
        """
        if monitor not in MONITOR_OPTIONS:
            raise SonarrError(f"unknown monitor option: {monitor!r}")
        if series_type not in SERIES_TYPES:
            raise SonarrError(f"unknown series type: {series_type!r}")
        payload = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "seasonFolder": season_folder,
            "seriesType": series_type,
            "addOptions": {
                "monitor": monitor,
                "searchForMissingEpisodes": search_on_add,
                "searchForCutoffUnmetEpisodes": False,
            },
        }
        response = await self._request("POST", "/series", json=payload)
        item = self._json(response)
        if not isinstance(item, dict):
            # The add may well have succeeded, but we cannot describe what was created.
            raise SonarrError("unexpected Sonarr response shape")
        return _library_series(item)

    async def calendar(self, start: datetime, end: datetime) -> list[SonarrCalendarEpisode]:
        """Episodes airing between two instants, series included.

        `includeSeries` so one call renders a whole week: without it every row would need
        a second request for the title, which is a page load hostage to a slow Sonarr.
        """
        response = await self._request(
            "GET",
            "/calendar",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "includeSeries": "true",
            },
        )
        episodes = []
        for item in self._json_list(response):
            if not isinstance(item, dict):
                continue
            series = item.get("series")
            series = series if isinstance(series, dict) else {}
            episode_id = int_or_none(item.get("id"))
            season_number = int_or_none(item.get("seasonNumber"))
            episode_number = int_or_none(item.get("episodeNumber"))
            if episode_id is None or season_number is None or episode_number is None:
                continue
            episodes.append(
                SonarrCalendarEpisode(
                    episode_id=episode_id,
                    series_id=int_or_none(item.get("seriesId")) or 0,
                    series_title=series.get("title") or "",
                    tvdb_id=int_or_none(series.get("tvdbId")),
                    season_number=season_number,
                    episode_number=episode_number,
                    title=item.get("title") or None,
                    air_date_utc=_air_date(item.get("airDateUtc")),
                    has_file=bool(item.get("hasFile", False)),
                    monitored=bool(item.get("monitored", False)),
                )
            )
        return episodes

    async def quality_profiles(self) -> list[tuple[int, str]]:
        response = await self._request("GET", "/qualityprofile")
        try:
            return [(item["id"], item["name"]) for item in self._json_list(response)]
        except (KeyError, TypeError) as exc:
            # An entry without id/name is as unusable as no answer at all, and this
            # feeds the Settings dropdowns rather than a page that can shrug it off.
            raise SonarrError("unexpected Sonarr quality-profile shape") from exc

    async def root_folders(self) -> list[str]:
        response = await self._request("GET", "/rootfolder")
        try:
            return [item["path"] for item in self._json_list(response)]
        except (KeyError, TypeError) as exc:
            raise SonarrError("unexpected Sonarr root-folder shape") from exc


__all__ = [
    "APP_NAME",
    "MONITOR_ALL",
    "MONITOR_FIRST_SEASON",
    "MONITOR_FUTURE",
    "MONITOR_NONE",
    "MONITOR_OPTIONS",
    "QUEUE_PAGE_SIZE",
    "SERIES_TYPES",
    "SERIES_TYPE_STANDARD",
    "SonarrAuthError",
    "SonarrCalendarEpisode",
    "SonarrClient",
    "SonarrConnectionError",
    "SonarrError",
    "SonarrLookupResult",
    "SonarrSeason",
    "SonarrSeries",
    "build_verify",
]
