"""Async Radarr v3 REST client (Step 9) — the app's reason to exist.

Every outbound call validates TLS by default. A self-signed home Radarr is
handled by pointing `BM_TLS_CA_FILE` at its CA, never by disabling verification;
`verify=False` is available only via the explicit `BM_OUTBOUND_TLS_VERIFY=false`
escape hatch and is discouraged. Failures map to typed errors so callers (the
pipeline, the Settings "Test Connection" button) can react precisely.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass

import httpx

API_PREFIX = "/api/v3"
REQUEST_TIMEOUT_SECONDS = 15.0
API_KEY_HEADER = "X-Api-Key"


class RadarrError(Exception):
    """Base class for all Radarr client failures."""


class RadarrAuthError(RadarrError):
    """Radarr rejected the API key (HTTP 401/403)."""


class RadarrConnectionError(RadarrError):
    """Could not reach Radarr, or its TLS certificate failed validation."""


@dataclass(frozen=True)
class RadarrMovie:
    tmdb_id: int
    title: str
    year: int | None
    has_file: bool
    imdb_id: str | None = None
    radarr_id: int | None = None  # Radarr's internal movie id (for upgrade/search)
    file_quality: str | None = None  # quality name of the downloaded file, if any
    quality_profile_id: int | None = None  # the profile Radarr currently targets
    # Radarr carries artwork on every library entry, which is what lets a title the
    # weekly chart never covered still render as a poster rather than a grey box.
    poster_url: str | None = None
    # What Radarr's own UI routes a film on — `/movie/{titleSlug}`, e.g.
    # "dune-part-two-693134". Read rather than composed, so a deep link into an instance
    # is the address that instance actually uses. Optional: a response without it simply
    # yields no link, which is what the chip did before there was one.
    title_slug: str | None = None


@dataclass(frozen=True)
class CreditPerson:
    """One cast or crew member. `role` is the character for cast, the job for crew."""

    name: str
    role: str | None
    headshot_url: str | None


@dataclass(frozen=True)
class MovieDetail:
    """Everything Radarr knows about one film, for the detail view.

    Cast and crew are absent unless the movie is in the library — Radarr only answers
    /credit for a movie it has added (its tmdbId filter is silently ignored).
    """

    tmdb_id: int
    title: str
    year: int | None
    overview: str | None
    poster_url: str | None
    backdrop_url: str | None
    genres: tuple[str, ...]
    imdb_id: str | None
    runtime_minutes: int | None
    certification: str | None
    studio: str | None
    original_language: str | None
    status: str | None
    website: str | None
    trailer_id: str | None
    ratings: tuple[tuple[str, str], ...]  # (source label, formatted score)
    cast: tuple[CreditPerson, ...] = ()
    crew: tuple[CreditPerson, ...] = ()


@dataclass(frozen=True)
class RadarrLookupResult:
    tmdb_id: int
    title: str
    year: int | None
    overview: str | None
    poster_url: str | None
    genres: tuple[str, ...]
    imdb_id: str | None
    rating: float | None = None


def build_verify(*, tls_verify: bool, ca_file: str | None) -> bool | str:
    """Translate config into httpx's `verify` argument."""
    if not tls_verify:
        return False
    if ca_file:
        return ca_file
    return True


def _image_from(images: list[dict], cover_type: str) -> str | None:
    for image in images:
        if image.get("coverType") == cover_type:
            return image.get("remoteUrl") or image.get("url")
    return None


# How each rating source is labelled and scaled in the detail view. Radarr carries all
# of these; showing them together is the point (one screen instead of four tabs).
_RATING_SOURCES = (
    ("imdb", "IMDb", "{:.1f}"),
    ("tmdb", "TMDB", "{:.1f}"),
    ("rottenTomatoes", "Rotten Tomatoes", "{:.0f}%"),
    ("metacritic", "Metacritic", "{:.0f}"),
)


def _ratings_from(ratings: dict) -> tuple[tuple[str, str], ...]:
    """Every source that actually reported a score, in a fixed order."""
    scored: list[tuple[str, str]] = []
    for key, label, template in _RATING_SOURCES:
        entry = ratings.get(key)
        if not isinstance(entry, dict) or entry.get("value") in (None, 0):
            continue
        scored.append((label, template.format(float(entry["value"]))))
    return tuple(scored)


# One page of the download queue. It decorates a grid of at most 100 cards; a queue
# deeper than this has a tail nobody is looking at, and an unbounded page is a request
# whose size Radarr decides.
QUEUE_PAGE_SIZE = 200


def _library_movie(item: dict) -> RadarrMovie:
    """One `/movie` entry -> RadarrMovie. Shared by the full list and the by-tmdb lookup
    so the two can never drift in what they read."""
    return RadarrMovie(
        tmdb_id=item.get("tmdbId", 0),
        title=item.get("title", ""),
        year=item.get("year"),
        has_file=bool(item.get("hasFile", False)),
        imdb_id=item.get("imdbId"),
        radarr_id=item.get("id"),
        file_quality=_file_quality(item),
        quality_profile_id=item.get("qualityProfileId"),
        poster_url=_image_from(item.get("images", []), "poster"),
        title_slug=item.get("titleSlug"),
    )


def _file_quality(item: dict) -> str | None:
    """The downloaded file's quality name, e.g. 'Bluray-1080p', if the movie has one."""
    movie_file = item.get("movieFile") or {}
    quality = movie_file.get("quality") or {}
    inner = quality.get("quality") or {}
    return inner.get("name")


def _rating_from(ratings: dict) -> float | None:
    # Radarr v3 ratings look like {"tmdb": {"value": 7.5}, "imdb": {"value": 8.1}} or
    # an older flat {"value": 7.5}. Prefer TMDB, then IMDb, then any generic value.
    for source in ("tmdb", "imdb"):
        entry = ratings.get(source)
        if isinstance(entry, dict) and entry.get("value") is not None:
            return float(entry["value"])
    if ratings.get("value") is not None:
        return float(ratings["value"])
    return None


class RadarrClient:
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
            # "unreachable" rather than 500-ing every page that touches Radarr.
            raise RadarrConnectionError(f"cannot reach Radarr at {self._base_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RadarrConnectionError(f"request to Radarr failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise RadarrAuthError("Radarr rejected the API key")
        if response.status_code >= 400:
            raise RadarrError(f"Radarr returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> object:
        """Parse a Radarr response body, mapping a non-JSON 200 to RadarrError.

        A proxy in front of Radarr can answer any path with an HTML login or error
        page; every caller catches RadarrError, while JSONDecodeError escapes to a
        500 (web) or an unrecorded crash (scheduler).
        """
        try:
            return response.json()
        except ValueError as exc:
            raise RadarrError("Radarr returned a non-JSON response") from exc

    @staticmethod
    def _json_list(response: httpx.Response) -> list:
        """Same, for the endpoints whose answer is iterated straight away.

        A bare `for item in ...` over a dict silently yields its KEYS, so a shape
        surprise would become wrong data rather than a caught error.
        """
        items = RadarrClient._json(response)
        if not isinstance(items, list):
            raise RadarrError("unexpected Radarr response shape")
        return items

    async def system_status(self) -> dict:
        """Used by the Settings 'Test Connection' button to prove reachability + key."""
        response = await self._request("GET", "/system/status")
        return self._json(response)

    async def queue(self) -> dict[int, float]:
        """How far along each queued download is, as a percentage, keyed by Radarr's own
        movie id.

        Keyed that way because it is what the queue reports and what a card already holds
        for the box it is on — the queue does not carry a TMDB id. Bounded by one page:
        this decorates a grid, and a queue longer than the page is a queue whose tail is
        not what anyone is looking at.

        Defensive in the same way the list endpoints are, and for the same reason — a
        proxy can answer this path with anything. A record Radarr has not sized yet is 0%,
        not a division by zero; several records for one film take the LOWEST, since a film
        is no nearer than its slowest part.
        """
        response = await self._request(
            "GET", "/queue", params={"pageSize": QUEUE_PAGE_SIZE}
        )
        payload = self._json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise RadarrError("unexpected Radarr queue response shape")
        progress: dict[int, float] = {}
        for record in payload["records"]:
            if not isinstance(record, dict):
                continue
            movie_id = record.get("movieId")
            size, left = record.get("size"), record.get("sizeleft")
            if not isinstance(movie_id, int) or isinstance(movie_id, bool):
                continue
            if not isinstance(size, int | float) or not isinstance(left, int | float):
                continue
            # max(0, ...) because Radarr reports sizeleft > size briefly while it revises
            # an estimate, and a negative percentage would render as a negative fill.
            percent = 0.0 if size <= 0 else max(0.0, min(100.0, 100 * (1 - left / size)))
            progress[movie_id] = min(progress.get(movie_id, percent), percent)
        return progress

    async def list_movies(self) -> list[RadarrMovie]:
        response = await self._request("GET", "/movie")
        return [_library_movie(item) for item in self._json_list(response)]

    async def movie_by_tmdb(self, tmdb_id: int) -> RadarrMovie | None:
        """One library entry by TMDB id, or None when Radarr does not have it.

        `GET /movie?tmdbId=` instead of pulling the whole library to answer a question
        about a single film. Verified against a live Radarr before being adopted: the
        filter returns exactly the requested film, an empty list for one it does not
        hold, and an unrecognised parameter still returns everything — the control that
        distinguishes a real filter from `/credit`'s silently ignored one.

        None means "Radarr answered, and does not have it". A failure to reach Radarr
        raises, so the caller can tell "not in the library" from "could not look".
        """
        response = await self._request("GET", "/movie", params={"tmdbId": tmdb_id})
        matches = self._json(response)
        if not isinstance(matches, list) or not matches:
            return None
        return _library_movie(matches[0])

    async def upgrade_movie(self, radarr_id: int, quality_profile_id: int) -> None:
        """Point an existing movie at a new quality profile and trigger a search.

        Used to fetch a different quality of a title already in the library — never
        to create a duplicate.
        """
        movie = self._json(await self._request("GET", f"/movie/{radarr_id}"))
        if not isinstance(movie, dict):
            # PUT below sends this back to Radarr; a non-object would rewrite the
            # movie record with whatever shape arrived.
            raise RadarrError("unexpected Radarr response shape")
        movie["qualityProfileId"] = quality_profile_id
        movie["monitored"] = True
        await self._request("PUT", f"/movie/{radarr_id}", json=movie)
        # The profile change above is the meaningful state change; kicking off the search
        # is best-effort — Radarr will pick the movie up on its own schedule anyway — so a
        # failed command must not report the (successful) upgrade as failed.
        try:
            await self._request(
                "POST", "/command", json={"name": "MoviesSearch", "movieIds": [radarr_id]}
            )
        except RadarrError:
            pass

    async def lookup(self, term: str) -> list[RadarrLookupResult]:
        response = await self._request("GET", "/movie/lookup", params={"term": term})
        return [
            RadarrLookupResult(
                tmdb_id=item.get("tmdbId", 0),
                title=item.get("title", ""),
                year=item.get("year"),
                overview=item.get("overview"),
                poster_url=_image_from(item.get("images", []), "poster"),
                genres=tuple(item.get("genres", [])),
                imdb_id=item.get("imdbId"),
                rating=_rating_from(item.get("ratings", {})),
            )
            for item in self._json_list(response)
        ]

    async def movie_detail(self, tmdb_id: int) -> MovieDetail:
        """One film by TMDB id — a direct lookup, not a title search."""
        response = await self._request(
            "GET", "/movie/lookup/tmdb", params={"tmdbId": tmdb_id}
        )
        # Everything below turns a surprising response into RadarrError. The caller
        # catches that and renders "details unavailable"; AttributeError or
        # JSONDecodeError would escape it and become a 500 page instead.
        item = self._json(response)
        # Radarr v3 answers this with an object (verified live), but the shape is not
        # contractual across versions. An empty list raises rather than becoming a blank
        # record: "no film" is a failure to answer, not an answer.
        if isinstance(item, list):
            item = item[0] if item else None
        if not isinstance(item, dict):
            raise RadarrError("unexpected /movie/lookup/tmdb response shape")
        images = item.get("images", [])
        language = item.get("originalLanguage") or {}
        return MovieDetail(
            tmdb_id=item.get("tmdbId", tmdb_id),
            title=item.get("title", ""),
            year=item.get("year"),
            overview=item.get("overview"),
            poster_url=_image_from(images, "poster"),
            backdrop_url=_image_from(images, "fanart"),
            genres=tuple(item.get("genres", [])),
            imdb_id=item.get("imdbId"),
            runtime_minutes=item.get("runtime") or None,
            certification=item.get("certification") or None,
            studio=item.get("studio") or None,
            original_language=language.get("name"),
            status=item.get("status"),
            website=item.get("website") or None,
            trailer_id=item.get("youTubeTrailerId") or None,
            ratings=_ratings_from(item.get("ratings", {})),
        )

    async def credits(self, radarr_id: int) -> tuple[tuple[CreditPerson, ...], ...]:
        """(cast, crew) for a movie in the library, ordered as Radarr returns them.

        Radarr only answers this for an added movie: passing tmdbId instead of movieId
        returns every credit in the database rather than an error, so the caller must
        already hold a real Radarr id.
        """
        response = await self._request("GET", "/credit", params={"movieId": radarr_id})
        cast: list[CreditPerson] = []
        crew: list[CreditPerson] = []
        for entry in self._json_list(response):
            person = CreditPerson(
                name=entry.get("personName", ""),
                role=entry.get("character") or entry.get("job") or None,
                headshot_url=_image_from(entry.get("images", []), "headshot"),
            )
            if not person.name:
                continue
            (cast if entry.get("type") == "cast" else crew).append(person)
        return tuple(cast), tuple(crew)

    async def add_movie(
        self,
        *,
        tmdb_id: int,
        title: str,
        year: int | None,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_on_add: bool = True,
        minimum_availability: str = "released",
    ) -> RadarrMovie:
        payload = {
            "tmdbId": tmdb_id,
            "title": title,
            "year": year,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "minimumAvailability": minimum_availability,
            "addOptions": {"searchForMovie": search_on_add},
        }
        response = await self._request("POST", "/movie", json=payload)
        item = self._json(response)
        if not isinstance(item, dict):
            # The add may well have succeeded, but we cannot describe what was created.
            raise RadarrError("unexpected Radarr response shape")
        return RadarrMovie(
            tmdb_id=item.get("tmdbId", tmdb_id),
            title=item.get("title", title),
            year=item.get("year", year),
            has_file=bool(item.get("hasFile", False)),
            imdb_id=item.get("imdbId"),
        )

    async def quality_profiles(self) -> list[tuple[int, str]]:
        response = await self._request("GET", "/qualityprofile")
        try:
            return [(item["id"], item["name"]) for item in self._json_list(response)]
        except (KeyError, TypeError) as exc:
            # An entry without id/name is as unusable as no answer at all, and this
            # feeds the Settings dropdowns rather than a page that can shrug it off.
            raise RadarrError("unexpected Radarr quality-profile shape") from exc

    async def root_folders(self) -> list[str]:
        response = await self._request("GET", "/rootfolder")
        try:
            return [item["path"] for item in self._json_list(response)]
        except (KeyError, TypeError) as exc:
            raise RadarrError("unexpected Radarr root-folder shape") from exc
