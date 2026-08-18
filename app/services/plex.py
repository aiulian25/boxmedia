"""What the Plex server already holds, so the review page can say so (Plex.md P1).

A chart title can be missing from every Radarr and still be sitting in Plex — ripped
years ago, added by hand, grabbed by another tool — and without this the weekly card
would happily let a second copy be added. The card's job is to inform, never to block:
Radarr managing a better copy of something Plex holds badly is a legitimate choice, so
nothing here removes an Add button.

Read-only by construction: the client knows two GET endpoints and no others. One
connection, not a list — multiple Radarrs exist because quality profiles differ per
instance, and there is no Plex analogue of that, so the store follows `user.yml`'s
single-record shape rather than `apps.yml`'s list.

Presence is answered in two tiers, because the evidence comes in two strengths: an id
match (Plex's own tmdb/imdb guids against the ids the card already carries) renders
confident; a title+year match is an educated guess and says so in the amber register
every other guess in this app uses. Presence is never written into a stored report —
it drifts exactly like library state, which is computed at render time for the same
reason.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core import crypto, filestore
from app.core.audit import AuditAction, AuditLog
from app.services.apps import API_KEY_MASK, InvalidAppError, normalize_url
from app.services.matcher import normalize_title
from app.services.radarr import build_verify

PLEX_SCHEMA_VERSION = 1
PLEX_FILENAME = "plex.yml"
SERVER_KEY = "server"

PLEX_CACHE_SCHEMA_VERSION = 1
PLEX_CACHE_FILENAME = "plex-library.json"
MOVIES_KEY = "movies"
FETCHED_AT_KEY = "fetched_at"
TRUNCATED_KEY = "truncated"

REQUEST_TIMEOUT_SECONDS = 10.0
# A page render must never hang on a slow media server; the fetch gets this long in
# total before the page falls back to the cached snapshot.
RENDER_FETCH_TIMEOUT_SECONDS = 6.0
# Refreshed at most this often by page renders; inside the window every render costs
# zero Plex requests. The Settings button bypasses it for "I just added a film".
PLEX_CACHE_TTL_SECONDS = 900.0
# 500 items per request is Plex's own web client's neighbourhood; 40 pages bounds the
# fetch at 20k movies. A bigger library is trimmed AND SAYS SO (`truncated`) — silent
# truncation would read as "covered everything" on exactly the library where it wasn't.
PLEX_PAGE_SIZE = 500
MAX_PLEX_PAGES = 40

# The two answers `PlexSnapshot.holds` can give, named so no caller matches on a bare
# string it happens to know. YES is an id match — Plex's own guid against the id the
# card carries. PROBABLY is a normalized-title-and-year match: the educated guess.
HOLDS_YES = "yes"
HOLDS_PROBABLY = "probably"

# Plex's modern agent lists ids as Guid children ("tmdb://603"); libraries still on the
# legacy agents carry one string like "com.plexapp.agents.imdb://tt0133093?lang=en".
# Both shapes are read; anything else contributes title+year only.
_TMDB_GUID_RE = re.compile(r"tmdb://(\d+)")
_IMDB_GUID_RE = re.compile(r"imdb(?:://|.{0,40}?//)(tt\d{7,9})(?!\d)")


class PlexError(Exception):
    """The Plex server could not be reached, or refused the token."""


class PlexAuthError(PlexError):
    """The token was rejected — reachable server, wrong credential."""


@dataclass(frozen=True)
class PlexServer:
    url: str
    token_encrypted: str

    def public(self) -> dict[str, object]:
        """View for templates — the token is masked, never revealed."""
        return {"url": self.url, "token_mask": API_KEY_MASK}


@dataclass(frozen=True)
class PlexMovie:
    """One library item, reduced to exactly what presence-checking needs."""

    title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None


@dataclass(frozen=True)
class PlexFetch:
    """A library listing, honest about whether the page cap trimmed it."""

    movies: tuple[PlexMovie, ...]
    truncated: bool


class PlexStore:
    """The single optional Plex connection, token encrypted at rest.

    One record, `user.yml`-style. The token goes through the same AES-GCM field
    encryption as the Radarr API keys and is only ever decrypted on the explicit
    `decrypt_token` path the client builder uses.
    """

    def __init__(self, config_dir: Path, *, key: bytes, audit: AuditLog | None = None) -> None:
        self._path = config_dir / PLEX_FILENAME
        self._key = key
        self._audit = audit

    def load(self) -> PlexServer | None:
        if not self._path.exists():
            return None
        document = filestore.read_yaml(self._path, expected_version=PLEX_SCHEMA_VERSION)
        stored = document.get(SERVER_KEY)
        if not isinstance(stored, dict):
            return None
        return PlexServer(
            url=str(stored.get("url", "")),
            token_encrypted=str(stored.get("token_encrypted", "")),
        )

    def save(self, *, url: str, token: str | None) -> PlexServer:
        """Create or update the connection.

        A blank token on an existing record keeps the stored one — the same "leave the
        key field empty to keep it" contract the Radarr cards honour, so editing the
        URL never forces re-pasting a credential.
        """
        normalized = normalize_url(url)
        existing = self.load()
        if token:
            token_encrypted = crypto.encrypt_field(token, self._key)
        elif existing is not None:
            token_encrypted = existing.token_encrypted
        else:
            raise InvalidAppError("a Plex token is required")
        server = PlexServer(url=normalized, token_encrypted=token_encrypted)
        filestore.write_yaml(
            self._path,
            {SERVER_KEY: {"url": server.url, "token_encrypted": server.token_encrypted}},
            schema_version=PLEX_SCHEMA_VERSION,
        )
        if self._audit:
            self._audit.record(AuditAction.PLEX_UPDATED, url=server.url)
        return server

    def remove(self) -> bool:
        if not self._path.exists():
            return False
        self._path.unlink()
        if self._audit:
            self._audit.record(AuditAction.PLEX_REMOVED)
        return True

    def decrypt_token(self) -> str:
        server = self.load()
        if server is None:
            raise PlexError("no Plex connection is configured")
        return crypto.decrypt_field(server.token_encrypted, self._key)

    def build_client(
        self, *, tls_verify: bool, ca_file: str | None, timeout: float | None = None
    ) -> PlexClient:
        server = self.load()
        if server is None:
            raise PlexError("no Plex connection is configured")
        return PlexClient(
            server.url,
            self.decrypt_token(),
            verify=build_verify(tls_verify=tls_verify, ca_file=ca_file),
            timeout=timeout or REQUEST_TIMEOUT_SECONDS,
        )


def client_for_credentials(
    url: str,
    token: str,
    *,
    tls_verify: bool,
    ca_file: str | None,
    timeout: float | None = None,
) -> PlexClient:
    """A Plex client for credentials, stored or not.

    Mirrors `apps.client_for_credentials`, and for the same reason: testing a
    connection before it is saved has to talk to exactly what saving it would talk to,
    so the address goes through the same `normalize_url` that `save` applies. Raises
    InvalidAppError for an address that cannot be parsed — the same answer `save` gives.
    """
    return PlexClient(
        normalize_url(url),
        token,
        verify=build_verify(tls_verify=tls_verify, ca_file=ca_file),
        timeout=timeout or REQUEST_TIMEOUT_SECONDS,
    )


class PlexClient:
    """Two GET endpoints and nothing else — sections, and a section's movies.

    The token travels as the `X-Plex-Token` HEADER, never a query parameter: a token in
    a URL is a token in every access log on the path between here and the server.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify: bool | str = True,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._verify = verify
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-Plex-Token": self._token, "Accept": "application/json"},
            verify=self._verify,
            timeout=self._timeout,
        )

    async def _get_json(self, client: httpx.AsyncClient, path: str, **params: object) -> dict:
        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PlexError(f"could not reach Plex: {exc}") from exc
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise PlexAuthError("Plex rejected the token")
        if response.is_error:
            raise PlexError(f"Plex answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise PlexError("Plex answered something that is not JSON") from exc

    async def movie_section_keys(self) -> list[str]:
        """Every movie library's section key — plural, because users split libraries
        (Movies, Kids, Concerts) and a film in any of them is still held."""
        async with self._client() as client:
            document = await self._get_json(client, "/library/sections")
        directories = document.get("MediaContainer", {}).get("Directory", [])
        return [
            str(entry["key"])
            for entry in directories
            if isinstance(entry, dict) and entry.get("type") == "movie" and "key" in entry
        ]

    async def list_movies(self) -> PlexFetch:
        """Everything the movie sections hold, paginated, capped, and honest about it."""
        movies: list[PlexMovie] = []
        truncated = False
        section_keys = await self.movie_section_keys()
        async with self._client() as client:
            for key in section_keys:
                pages = 0
                start = 0
                while True:
                    if pages >= MAX_PLEX_PAGES:
                        truncated = True
                        break
                    document = await self._get_json(
                        client,
                        f"/library/sections/{key}/all",
                        type=1,
                        includeGuids=1,
                        **{
                            "X-Plex-Container-Start": start,
                            "X-Plex-Container-Size": PLEX_PAGE_SIZE,
                        },
                    )
                    container = document.get("MediaContainer", {})
                    items = container.get("Metadata", []) or []
                    movies.extend(_movie_from_item(item) for item in items)
                    pages += 1
                    start += len(items)
                    total = container.get("totalSize", container.get("size", 0))
                    if not items or start >= int(total or 0):
                        break
        return PlexFetch(movies=tuple(movies), truncated=truncated)


def _movie_from_item(item: dict[str, Any]) -> PlexMovie:
    """One Plex item reduced to ids and identity, whichever agent catalogued it."""
    guid_texts = [str(item.get("guid", ""))]
    guid_texts += [
        str(child.get("id", "")) for child in item.get("Guid", []) if isinstance(child, dict)
    ]
    joined = " ".join(guid_texts)
    tmdb_match = _TMDB_GUID_RE.search(joined)
    imdb_match = _IMDB_GUID_RE.search(joined)
    year = item.get("year")
    return PlexMovie(
        title=str(item.get("title", "")),
        year=int(year) if isinstance(year, int) else None,
        tmdb_id=int(tmdb_match.group(1)) if tmdb_match else None,
        imdb_id=imdb_match.group(1) if imdb_match else None,
    )


@dataclass(frozen=True)
class PlexSnapshot:
    """The library resolved once, for answering many titles without re-asking Plex.

    The `IgnoreSnapshot` shape, for the `IgnoreSnapshot` reason: every card on a page
    is judged against the same library, not against whatever Plex held at the moment
    that row happened to be built.
    """

    tmdb_ids: frozenset[int]
    imdb_ids: frozenset[str]
    # normalized title -> the years Plex holds it under. A film with no year in Plex
    # contributes None, which matches any asked year — absence of evidence is not a
    # conflicting year.
    title_years: dict[str, frozenset[int | None]]
    truncated: bool = False

    def holds(
        self,
        tmdb_id: int | None,
        imdb_id: str | None,
        title: str,
        year: int | None,
    ) -> str | None:
        """Whether Plex holds this film: HOLDS_YES, HOLDS_PROBABLY, or None.

        Ids answer first and alone — they are exact, and a guid match with a different
        spelling is still the same film. Titles answer only with the year's consent:
        two films sharing a normalized title is the remake trap, and claiming the 1970
        original covers the 2026 remake would be this feature causing the exact
        double-take it exists to prevent.
        """
        if tmdb_id is not None and tmdb_id in self.tmdb_ids:
            return HOLDS_YES
        if imdb_id is not None and imdb_id in self.imdb_ids:
            return HOLDS_YES
        years = self.title_years.get(normalize_title(title))
        if years is None:
            return None
        if year is None or None in years or year in years:
            return HOLDS_PROBABLY
        return None


def snapshot_from_movies(movies: tuple[PlexMovie, ...], *, truncated: bool = False) -> PlexSnapshot:
    title_years: dict[str, set[int | None]] = {}
    for movie in movies:
        if movie.title:
            title_years.setdefault(normalize_title(movie.title), set()).add(movie.year)
    return PlexSnapshot(
        tmdb_ids=frozenset(m.tmdb_id for m in movies if m.tmdb_id is not None),
        imdb_ids=frozenset(m.imdb_id for m in movies if m.imdb_id is not None),
        title_years={title: frozenset(years) for title, years in title_years.items()},
        truncated=truncated,
    )


class PlexLibraryCache:
    """The last fetched library on disk, so renders inside the TTL cost Plex nothing.

    A cache, not a record: unreadable, hand-edited or written by a newer build all read
    as empty, because losing it costs one refetch and refusing to render costs the page.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / PLEX_CACHE_FILENAME

    def save(self, fetch: PlexFetch) -> None:
        filestore.write_json(
            self._path,
            {
                FETCHED_AT_KEY: time.time(),
                TRUNCATED_KEY: fetch.truncated,
                MOVIES_KEY: [
                    {"title": m.title, "year": m.year, "tmdb_id": m.tmdb_id, "imdb_id": m.imdb_id}
                    for m in fetch.movies
                ],
            },
            schema_version=PLEX_CACHE_SCHEMA_VERSION,
        )

    def load(self) -> tuple[PlexSnapshot, float] | None:
        """The cached snapshot and when it was fetched, or None when there is none.

        Age is the CALLER's decision: the render path wants a fresh one but will take
        stale over nothing when Plex is down, and the Refresh button wants none at all.
        """
        if not self._path.exists():
            return None
        try:
            document = filestore.read_json(
                self._path, expected_version=PLEX_CACHE_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            return None
        stored = document.get(MOVIES_KEY)
        if not isinstance(stored, list):
            return None
        movies = tuple(
            PlexMovie(
                title=str(entry.get("title", "")),
                year=entry.get("year") if isinstance(entry.get("year"), int) else None,
                tmdb_id=entry.get("tmdb_id") if isinstance(entry.get("tmdb_id"), int) else None,
                imdb_id=entry.get("imdb_id") if isinstance(entry.get("imdb_id"), str) else None,
            )
            for entry in stored
            if isinstance(entry, dict)
        )
        truncated = bool(document.get(TRUNCATED_KEY, False))
        fetched_at = document.get(FETCHED_AT_KEY)
        age_anchor = float(fetched_at) if isinstance(fetched_at, int | float) else 0.0
        return snapshot_from_movies(movies, truncated=truncated), age_anchor

    def forget(self) -> None:
        """Drop the snapshot when the connection is removed — a library nobody is
        connected to must not keep decorating cards."""
        self._path.unlink(missing_ok=True)
