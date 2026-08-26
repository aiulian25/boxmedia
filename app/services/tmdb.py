"""Async TMDB v3 client (TV step 6) — artwork, descriptions, and the TVDB bridge.

TMDB does two jobs for television. It is where every poster and overview comes from,
and it is the only way to turn the TMDB id a discovery row carries into the **TVDB id**
Sonarr keys series on. `tv_detail` does both in ONE round trip by asking TMDB to append
the extras, rather than the six concurrent calls the teardown found nzb360 making
(`tv-discovery.md` §6): we are server-side, so one request that returns everything beats
six that each pay their own latency.

Shaped like `radarr.py` and `sonarr.py` — one `_request` mapping failures to typed
errors, readers that refuse a surprising body, TLS verified by default — with two
differences that are TMDB's, not ours:

* **The key is a query parameter.** Their v3 contract offers no header form. Every
  message this module builds therefore goes through `discovery.redact` before it can
  become an exception, a log line or a page. That helper is value-blind, so it covers a
  key that was rotated a second ago and one that was never stored at all.
* **They rate-limit.** A 429 carrying `Retry-After` is honoured ONCE, and only for a
  short wait — a page must not hang because a public API is busy. Anything longer, or a
  second 429, gives up loudly rather than sleeping through it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import httpx

from app.services.discovery import (
    TMDB_BASE_URL,
    TMDB_IMAGE_BASE_URL,
    USER_AGENT,
    redact,
    scrub,
)
from app.services.posters import HEADSHOT_WIDTH, POSTER_WIDTH

REQUEST_TIMEOUT_SECONDS = 10.0
# TMDB answers in the language you ask for. One constant so a future locale is a single
# edit rather than a hunt through call sites — the app is en-only today (ruling 9).
DEFAULT_LANGUAGE = "en-US"
# Certifications are per country. US is the one TMDB has for nearly every show; anything
# else falls back to whatever the show does carry rather than showing nothing.
PREFERRED_CERTIFICATION_COUNTRY = "US"
BACKDROP_WIDTH = "w780"

# A 429 is honoured once and briefly. Longer than this and the honest answer is "TMDB is
# rate-limiting us" — sleeping 30s inside a page render is not a retry, it is a hang.
MAX_RETRY_AFTER_SECONDS = 5.0
DEFAULT_RETRY_AFTER_SECONDS = 1.0

# What Discover's filter bar can actually set. Deliberately a fraction of the 25 filters
# `/discover/tv` accepts (tv-discovery.md §4.1): every one of them is a control somebody
# has to understand, and these four answer the questions the mockup's bar asks.
SORT_POPULARITY = "popularity.desc"
SORT_FIRST_AIR_DATE = "first_air_date.desc"
SORT_RATING = "vote_average.desc"
SORT_OPTIONS = (SORT_POPULARITY, SORT_FIRST_AIR_DATE, SORT_RATING)


class TmdbError(Exception):
    """Base class for all TMDB client failures."""


class TmdbAuthError(TmdbError):
    """TMDB rejected the API key (HTTP 401/403)."""


class TmdbConnectionError(TmdbError):
    """Could not reach TMDB, or its TLS certificate failed validation."""


class TmdbRateLimitError(TmdbError):
    """TMDB is rate-limiting us and the wait is longer than a page should hold."""


@dataclass(frozen=True)
class TmdbPerson:
    """One cast member. `role` is the character, matching `radarr.CreditPerson`."""

    name: str
    role: str | None
    headshot_url: str | None


@dataclass(frozen=True)
class TmdbSeason:
    season_number: int
    name: str
    episode_count: int
    air_date: date | None
    overview: str | None
    poster_url: str | None


@dataclass(frozen=True)
class TmdbShow:
    """One row from discover or search — enough to render a card and decide on it."""

    tmdb_id: int
    title: str
    first_air_date: date | None
    overview: str | None
    poster_url: str | None
    backdrop_url: str | None
    rating: float | None
    original_language: str | None = None

    @property
    def year(self) -> int | None:
        return self.first_air_date.year if self.first_air_date else None


@dataclass(frozen=True)
class TmdbShowDetail:
    """Everything the detail page needs, from one request.

    `tvdb_id` is the reason this class exists in the shape it does: it is the only thing
    that makes a Sonarr add possible from a TMDB-sourced row, and it arrives here rather
    than from a second call.
    """

    tmdb_id: int
    title: str
    first_air_date: date | None
    overview: str | None
    poster_url: str | None
    backdrop_url: str | None
    rating: float | None
    genres: tuple[str, ...]
    networks: tuple[str, ...]
    status: str | None
    episode_run_time: int | None
    number_of_seasons: int | None
    number_of_episodes: int | None
    in_production: bool
    # The bridge. None means TMDB has no TVDB id for this show, which is an honest
    # "cannot add to Sonarr" rather than something to paper over with a guess.
    tvdb_id: int | None
    imdb_id: str | None
    certification: str | None
    trailer_url: str | None
    seasons: tuple[TmdbSeason, ...]
    cast: tuple[TmdbPerson, ...]

    @property
    def year(self) -> int | None:
        return self.first_air_date.year if self.first_air_date else None

    @property
    def addable(self) -> bool:
        """Whether Sonarr could take this show at all."""
        return self.tvdb_id is not None


def image_url(path: object, width: str = POSTER_WIDTH) -> str | None:
    """A TMDB image path (`/abc.jpg`) as a full URL at the size we actually render.

    Composed at POSTER_WIDTH rather than `original` for the reason `posters.sized`
    documents: the cache keys on the URL, so two forms of the same image would be two
    entries, and `original` is a 1-3 MB file painted into a 208px box. The result passes
    through `sized` unchanged, so callers may keep routing through it.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return f"{TMDB_IMAGE_BASE_URL}/{width}{path}"


def _int_or_none(value: object) -> int | None:
    """A real integer, or None. `bool` is an int in Python and would become 0/1."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _air_date(value: object) -> date | None:
    """TMDB's `YYYY-MM-DD`, or None.

    Empty string is TMDB's answer for a show with no announced date, and it is common —
    an anticipated series has no first air date yet. None, never an exception: one
    undated show must not blank a row.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _names(items: object, key: str = "name") -> tuple[str, ...]:
    """The `name` of each entry in a TMDB list-of-objects, skipping anything odd."""
    if not isinstance(items, list):
        return ()
    return tuple(
        entry[key]
        for entry in items
        if isinstance(entry, dict) and isinstance(entry.get(key), str) and entry[key]
    )


def _show_from(item: dict) -> TmdbShow:
    """One `/discover/tv` or `/search/tv` row -> TmdbShow. Shared so the two can never
    drift in what they read — `radarr._library_movie`'s reason."""
    return TmdbShow(
        tmdb_id=_int_or_none(item.get("id")) or 0,
        # TMDB calls a series' title `name`; `title` is the movie field. Both are read so
        # a row from a multi-search still lands with something in it.
        title=item.get("name") or item.get("title") or "",
        first_air_date=_air_date(item.get("first_air_date")),
        overview=item.get("overview") or None,
        poster_url=image_url(item.get("poster_path")),
        backdrop_url=image_url(item.get("backdrop_path"), BACKDROP_WIDTH),
        rating=_float_or_none(item.get("vote_average")),
        original_language=item.get("original_language") or None,
    )


def _certification(content_ratings: object) -> str | None:
    """The age rating, preferring the US one and falling back to any.

    Falling back rather than showing nothing: a British or Japanese show often carries no
    US rating at all, and "TV-MA or whatever your country calls it" is more use than a
    blank.
    """
    results = content_ratings.get("results") if isinstance(content_ratings, dict) else None
    if not isinstance(results, list):
        return None
    ratings = [
        entry for entry in results
        if isinstance(entry, dict) and isinstance(entry.get("rating"), str) and entry["rating"]
    ]
    for entry in ratings:
        if entry.get("iso_3166_1") == PREFERRED_CERTIFICATION_COUNTRY:
            return entry["rating"]
    return ratings[0]["rating"] if ratings else None


def _trailer_url(videos: object) -> str | None:
    """The first YouTube trailer, as a watchable link.

    A link out, never an embed: an iframe would need `frame-src` opened up in the CSP,
    and this app's whole poster cache exists so that policy can stay shut.
    """
    results = videos.get("results") if isinstance(videos, dict) else None
    if not isinstance(results, list):
        return None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if entry.get("site") == "YouTube" and entry.get("type") == "Trailer" and key:
            return f"https://www.youtube.com/watch?v={key}"
    return None


def _seasons(items: object) -> tuple[TmdbSeason, ...]:
    if not isinstance(items, list):
        return ()
    seasons = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        number = _int_or_none(entry.get("season_number"))
        if number is None:
            continue
        seasons.append(
            TmdbSeason(
                season_number=number,
                name=entry.get("name") or f"Season {number}",
                episode_count=_int_or_none(entry.get("episode_count")) or 0,
                air_date=_air_date(entry.get("air_date")),
                overview=entry.get("overview") or None,
                poster_url=image_url(entry.get("poster_path")),
            )
        )
    return tuple(seasons)


def _cast(credits: object, limit: int) -> tuple[TmdbPerson, ...]:
    people = credits.get("cast") if isinstance(credits, dict) else None
    if not isinstance(people, list):
        return ()
    out = []
    for entry in people[:limit]:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        out.append(
            TmdbPerson(
                name=entry["name"],
                role=entry.get("character") or None,
                headshot_url=image_url(entry.get("profile_path"), HEADSHOT_WIDTH),
            )
        )
    return tuple(out)


# How many cast members the detail page shows. TMDB returns the full unit — dozens for a
# long-running series — and the row scrolls, so this is about the payload, not the layout.
CAST_LIMIT = 12


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        *,
        verify: bool | str = True,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._api_key = api_key.strip()
        self._verify = verify
        self._timeout = timeout
        self._language = language

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=TMDB_BASE_URL,
            timeout=self._timeout,
            verify=self._verify,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def _request(self, path: str, params: dict | None = None) -> object:
        """One GET, with the key attached and one rate-limit retry.

        Every failure message goes through `redact`: the key is in the query string
        because TMDB's v3 API says so, and httpx puts the request URL into the text of
        most of its exceptions.

        The transport errors below raise `from None` AND `scrub` the original. `from
        None` alone only suppresses the printed traceback: `__context__` still holds the
        original object, whose text and whose `.request.url` both carry the key. A test
        walks that chain to prove neither survives.
        """
        query = {
            "api_key": self._api_key,
            "language": self._language,
            **{key: value for key, value in (params or {}).items() if value is not None},
        }
        for attempt in (1, 2):
            try:
                async with self._client() as client:
                    response = await client.get(path, params=query)
            except (httpx.ConnectError, OSError) as exc:
                # OSError covers ssl.SSLError and a misconfigured CA path raised while
                # building the SSL context — degrade to "unreachable" rather than 500-ing
                # every page that touches TMDB.
                raise TmdbConnectionError(f"cannot reach TMDB: {redact(scrub(exc))}") from None
            except httpx.HTTPError as exc:
                raise TmdbConnectionError(
                    f"request to TMDB failed: {redact(scrub(exc))}"
                ) from None

            if response.status_code == 429 and attempt == 1:
                await asyncio.sleep(_retry_after(response))
                continue
            break

        if response.status_code in (401, 403):
            raise TmdbAuthError("TMDB rejected the API key")
        if response.status_code == 429:
            # Loudly, rather than sleeping again: the caller renders "try again shortly",
            # which is true, instead of holding a request open hoping.
            raise TmdbRateLimitError("TMDB is rate-limiting this key — try again shortly")
        if response.status_code >= 400:
            raise TmdbError(f"TMDB returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            # Chained, unlike the transport errors above: a JSON decode error names a
            # position in the BODY, which carries no credential, and the position is
            # what makes it diagnosable.
            raise TmdbError("TMDB returned a non-JSON response") from exc

    async def configuration(self) -> dict:
        """Proves the key works. The cheapest authenticated call TMDB offers."""
        payload = await self._request("/configuration")
        if not isinstance(payload, dict):
            raise TmdbError("unexpected TMDB configuration response shape")
        return payload

    async def discover_tv(
        self,
        *,
        genre_id: int | None = None,
        first_air_date_from: date | None = None,
        sort_by: str = SORT_POPULARITY,
        original_language: str | None = None,
        page: int = 1,
    ) -> list[TmdbShow]:
        """Browse TMDB's catalogue with the handful of filters Discover exposes."""
        if sort_by not in SORT_OPTIONS:
            raise TmdbError(f"unknown sort option: {sort_by!r}")
        rows = await self._request(
            "/discover/tv",
            {
                "sort_by": sort_by,
                "page": page,
                "with_genres": genre_id,
                "first_air_date.gte": (
                    first_air_date_from.isoformat() if first_air_date_from else None
                ),
                "with_original_language": original_language,
                # Never negotiable, and never a setting: this app does not surface adult
                # content, and a filter you can turn off is one that gets turned off.
                "include_adult": "false",
            },
        )
        return _rows(rows)

    async def search_tv(self, query: str, *, page: int = 1) -> list[TmdbShow]:
        """Find a series by name — the fallback when a discovery row cannot be matched."""
        term = query.strip()
        if not term:
            return []
        rows = await self._request(
            "/search/tv", {"query": term, "page": page, "include_adult": "false"}
        )
        return _rows(rows)

    async def tv_detail(self, tmdb_id: int) -> TmdbShowDetail:
        """One show, complete, in ONE request.

        `append_to_response` is what makes that possible: external ids (the TVDB bridge),
        the age rating, the cast and the trailer all arrive with the show itself. The
        teardown found nzb360 fanning six concurrent calls out for this from a phone;
        server-side there is no reason to pay that six times over.
        """
        item = await self._request(
            f"/tv/{tmdb_id}",
            {"append_to_response": "external_ids,content_ratings,credits,videos"},
        )
        if not isinstance(item, dict):
            raise TmdbError("unexpected TMDB detail response shape")
        external = item.get("external_ids")
        external = external if isinstance(external, dict) else {}
        run_times = item.get("episode_run_time")
        run_time = None
        if isinstance(run_times, list) and run_times:
            run_time = _int_or_none(run_times[0])
        return TmdbShowDetail(
            tmdb_id=_int_or_none(item.get("id")) or tmdb_id,
            title=item.get("name") or "",
            first_air_date=_air_date(item.get("first_air_date")),
            overview=item.get("overview") or None,
            poster_url=image_url(item.get("poster_path")),
            backdrop_url=image_url(item.get("backdrop_path"), BACKDROP_WIDTH),
            rating=_float_or_none(item.get("vote_average")),
            genres=_names(item.get("genres")),
            networks=_names(item.get("networks")),
            status=item.get("status") or None,
            episode_run_time=run_time,
            number_of_seasons=_int_or_none(item.get("number_of_seasons")),
            number_of_episodes=_int_or_none(item.get("number_of_episodes")),
            in_production=bool(item.get("in_production", False)),
            tvdb_id=_int_or_none(external.get("tvdb_id")),
            imdb_id=external.get("imdb_id") or None,
            certification=_certification(item.get("content_ratings")),
            trailer_url=_trailer_url(item.get("videos")),
            seasons=_seasons(item.get("seasons")),
            cast=_cast(item.get("credits"), CAST_LIMIT),
        )

    async def tvdb_id_for(self, tmdb_id: int) -> int | None:
        """Just the bridge, for an add that needs nothing else.

        `/tv/{id}/external_ids` rather than the whole detail: an Add pressed on a
        discovery card already has the title and the poster, and this is a smaller
        answer than re-fetching them.
        """
        payload = await self._request(f"/tv/{tmdb_id}/external_ids")
        if not isinstance(payload, dict):
            raise TmdbError("unexpected TMDB external-ids response shape")
        return _int_or_none(payload.get("tvdb_id"))


def _rows(payload: object) -> list[TmdbShow]:
    """The `results` list of a paged TMDB response.

    A bare `for item in ...` over a dict silently yields its KEYS, so a shape surprise
    would become wrong data rather than a caught error.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise TmdbError("unexpected TMDB response shape")
    return [_show_from(item) for item in results if isinstance(item, dict)]


def _retry_after(response: httpx.Response) -> float:
    """How long TMDB asked us to wait, bounded to something a page can survive.

    A missing or unreadable header is a short default rather than zero: hammering a
    server that just said "slow down" is how a soft limit becomes a hard one.
    """
    raw = response.headers.get("Retry-After", "")
    try:
        wanted = float(raw)
    except (TypeError, ValueError):
        wanted = DEFAULT_RETRY_AFTER_SECONDS
    return max(0.0, min(wanted, MAX_RETRY_AFTER_SECONDS))
