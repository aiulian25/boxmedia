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

MEDIA_SERVER_SCHEMA_VERSION = 1
MEDIA_SERVER_FILENAME = "mediaserver.yml"
# What 1.1.0 wrote, when Plex was the only kind. Read on load so an existing install
# keeps working with no action; the next save writes the new name and removes this one.
LEGACY_PLEX_FILENAME = "plex.yml"
SERVER_KEY = "server"

# Which server this connection is. The store owns its vocabulary the way users.py owns
# THEMES: read-tolerant on load (an unknown kind is a Plex install from before kinds
# existed), write-strict in the form.
KIND_PLEX = "plex"
KIND_JELLYFIN = "jellyfin"
MEDIA_SERVER_KINDS = frozenset({KIND_PLEX, KIND_JELLYFIN})
# What the cards, verdicts and labels call each one. One place, so a chip and a test
# result can never disagree about the name of the thing they are describing.
SERVER_NAMES = {KIND_PLEX: "Plex", KIND_JELLYFIN: "Jellyfin"}
# What each kind calls the secret. Plex issues a token; Jellyfin issues an API key from
# Dashboard -> API Keys, and calling it a token there sends people looking for the wrong
# screen.
SECRET_LABELS = {KIND_PLEX: "Plex token", KIND_JELLYFIN: "API key"}

LIBRARY_CACHE_SCHEMA_VERSION = 1
LIBRARY_CACHE_FILENAME = "media-server-library.json"
MOVIES_KEY = "movies"
FETCHED_AT_KEY = "fetched_at"
TRUNCATED_KEY = "truncated"

REQUEST_TIMEOUT_SECONDS = 10.0
# A page render must never hang on a slow media server; the fetch gets this long in
# total before the page falls back to the cached snapshot.
RENDER_FETCH_TIMEOUT_SECONDS = 6.0
# Refreshed at most this often by page renders; inside the window every render costs
# zero Plex requests. The Settings button bypasses it for "I just added a film".
LIBRARY_CACHE_TTL_SECONDS = 900.0
# 500 items per request is Plex's own web client's neighbourhood; 40 pages bounds the
# fetch at 20k movies. A bigger library is trimmed AND SAYS SO (`truncated`) — silent
# truncation would read as "covered everything" on exactly the library where it wasn't.
PLEX_PAGE_SIZE = 500
# Jellyfin pages the same way through StartIndex/Limit against TotalRecordCount.
JELLYFIN_PAGE_SIZE = 500
MAX_LIBRARY_PAGES = 40

# The two answers `MediaServerSnapshot.holds` can give, named so no caller matches on a bare
# string it happens to know. YES is an id match — Plex's own guid against the id the
# card carries. PROBABLY is a normalized-title-and-year match: the educated guess.
HOLDS_YES = "yes"
HOLDS_PROBABLY = "probably"

# Plex's modern agent lists ids as Guid children ("tmdb://603"); libraries still on the
# legacy agents carry one string like "com.plexapp.agents.imdb://tt0133093?lang=en".
# Both shapes are read; anything else contributes title+year only.
_TMDB_GUID_RE = re.compile(r"tmdb://(\d+)")
_IMDB_GUID_RE = re.compile(r"imdb(?:://|.{0,40}?//)(tt\d{7,9})(?!\d)")
# The shape of an IMDb title id, for a source that hands one over whole rather than
# inside a URI. Same bound as the guid pattern above.
_IMDB_ID_RE = re.compile(r"tt\d{7,9}")


def validated_kind(value: object) -> str:
    """A stored kind, or Plex for anything this build does not ship.

    Read-tolerant, write-strict — the `users._validated_theme` pattern. A file written
    before kinds existed has no kind at all and must load as what it is: a Plex
    connection. The Settings form refuses an unknown value outright.
    """
    return value if isinstance(value, str) and value in MEDIA_SERVER_KINDS else KIND_PLEX


class MediaServerError(Exception):
    """The media server could not be reached, or refused the credential."""


class MediaServerAuthError(MediaServerError):
    """The credential was rejected — reachable server, wrong secret."""


class PlexError(MediaServerError):
    """The Plex server could not be reached, or refused the token."""


class PlexAuthError(PlexError, MediaServerAuthError):
    """The token was rejected — reachable server, wrong credential."""


class JellyfinError(MediaServerError):
    """The Jellyfin server could not be reached, or refused the API key."""


class JellyfinAuthError(JellyfinError, MediaServerAuthError):
    """The API key was rejected — reachable server, wrong credential."""


@dataclass(frozen=True)
class MediaServerConnection:
    url: str
    token_encrypted: str
    kind: str = KIND_PLEX

    @property
    def name(self) -> str:
        """What to call this server on a card, a verdict or a label."""
        return SERVER_NAMES.get(self.kind, SERVER_NAMES[KIND_PLEX])

    def public(self) -> dict[str, object]:
        """View for templates — the secret is masked, never revealed."""
        return {
            "url": self.url,
            "token_mask": API_KEY_MASK,
            "kind": self.kind,
            "name": self.name,
            "secret_label": SECRET_LABELS.get(self.kind, SECRET_LABELS[KIND_PLEX]),
        }


@dataclass(frozen=True)
class MediaServerMovie:
    """One library item, reduced to exactly what presence-checking needs."""

    title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None


@dataclass(frozen=True)
class MediaServerFetch:
    """A library listing, honest about whether the page cap trimmed it."""

    movies: tuple[MediaServerMovie, ...]
    truncated: bool


class MediaServerStore:
    """The single optional Plex connection, token encrypted at rest.

    One record, `user.yml`-style. The token goes through the same AES-GCM field
    encryption as the Radarr API keys and is only ever decrypted on the explicit
    `decrypt_token` path the client builder uses.
    """

    def __init__(self, config_dir: Path, *, key: bytes, audit: AuditLog | None = None) -> None:
        self._path = config_dir / MEDIA_SERVER_FILENAME
        self._legacy_path = config_dir / LEGACY_PLEX_FILENAME
        self._key = key
        self._audit = audit

    def load(self) -> MediaServerConnection | None:
        """The stored connection, reading the pre-kinds file when that is all there is.

        A 1.1.0 install has `plex.yml` and no kind. It loads as Plex and keeps working
        with no action from anyone; `save` is what migrates the file.
        """
        path = self._path if self._path.exists() else self._legacy_path
        if not path.exists():
            return None
        document = filestore.read_yaml(path, expected_version=MEDIA_SERVER_SCHEMA_VERSION)
        stored = document.get(SERVER_KEY)
        if not isinstance(stored, dict):
            return None
        return MediaServerConnection(
            url=str(stored.get("url", "")),
            token_encrypted=str(stored.get("token_encrypted", "")),
            kind=validated_kind(stored.get("kind")),
        )

    def save(
        self, *, url: str, token: str | None, kind: str = KIND_PLEX
    ) -> MediaServerConnection:
        """Create or update the connection.

        A blank token on an existing record keeps the stored one — the same "leave the
        key field empty to keep it" contract the Radarr cards honour, so editing the
        URL never forces re-pasting a credential.
        """
        normalized = normalize_url(url)
        existing = self.load()
        if kind not in MEDIA_SERVER_KINDS:
            raise InvalidAppError(f"unknown media server: {kind!r}")
        if token:
            token_encrypted = crypto.encrypt_field(token, self._key)
        elif existing is not None and existing.kind == kind:
            token_encrypted = existing.token_encrypted
        else:
            # A blank secret keeps the stored one only for the SAME server. Switching
            # kind and reusing the old secret would send a Plex token to Jellyfin.
            raise InvalidAppError(f"a {SECRET_LABELS.get(kind, 'credential')} is required")
        server = MediaServerConnection(
            url=normalized, token_encrypted=token_encrypted, kind=kind
        )
        filestore.write_yaml(
            self._path,
            {SERVER_KEY: {
                "url": server.url,
                "token_encrypted": server.token_encrypted,
                "kind": server.kind,
            }},
            schema_version=MEDIA_SERVER_SCHEMA_VERSION,
        )
        # The pre-kinds file has been superseded; leaving it would make the next load
        # depend on which of two files it happened to read.
        self._legacy_path.unlink(missing_ok=True)
        if self._audit:
            self._audit.record(
                AuditAction.MEDIA_SERVER_UPDATED, url=server.url, kind=server.kind
            )
        return server

    def remove(self) -> bool:
        if not (self._path.exists() or self._legacy_path.exists()):
            return False
        self._path.unlink(missing_ok=True)
        self._legacy_path.unlink(missing_ok=True)
        if self._audit:
            self._audit.record(AuditAction.MEDIA_SERVER_REMOVED)
        return True

    def decrypt_token(self) -> str:
        server = self.load()
        if server is None:
            raise PlexError("no Plex connection is configured")
        return crypto.decrypt_field(server.token_encrypted, self._key)

    def build_client(
        self, *, tls_verify: bool, ca_file: str | None, timeout: float | None = None
    ):  # noqa: ANN201 — PlexClient | JellyfinClient, both structurally identical
        server = self.load()
        if server is None:
            raise MediaServerError("no media server is configured")
        return client_for_credentials(
            server.url,
            self.decrypt_token(),
            kind=server.kind,
            tls_verify=tls_verify,
            ca_file=ca_file,
            timeout=timeout,
        )


def client_for_credentials(
    url: str,
    token: str,
    *,
    kind: str = KIND_PLEX,
    tls_verify: bool,
    ca_file: str | None,
    timeout: float | None = None,
):  # noqa: ANN201 — PlexClient | JellyfinClient
    """A client for credentials, stored or not, for whichever server this is.

    Mirrors `apps.client_for_credentials`, and for the same reason: testing a
    connection before it is saved has to talk to exactly what saving it would talk to,
    so the address goes through the same `normalize_url` that `save` applies. Raises
    InvalidAppError for an address that cannot be parsed — the same answer `save` gives.
    """
    client_class = JellyfinClient if kind == KIND_JELLYFIN else PlexClient
    return client_class(
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

    async def list_movies(self) -> MediaServerFetch:
        """Everything the movie sections hold, paginated, capped, and honest about it."""
        movies: list[MediaServerMovie] = []
        truncated = False
        section_keys = await self.movie_section_keys()
        async with self._client() as client:
            for key in section_keys:
                pages = 0
                start = 0
                while True:
                    if pages >= MAX_LIBRARY_PAGES:
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
        return MediaServerFetch(movies=tuple(movies), truncated=truncated)


class JellyfinClient:
    """One GET endpoint and nothing else — the movie list, optionally counted only.

    The API key travels as the `X-Emby-Token` HEADER. Jellyfin also accepts `?api_key=`
    in the query string and we never use it, for the reason the Plex client does not:
    a credential in a URL is a credential in every access log on the path.
    """

    ITEMS_PATH = "/Items"
    # Everything the presence check needs and nothing it does not. Images and user data
    # are the bulk of an unfiltered response and neither is ever read here.
    FIELDS = "ProviderIds,ProductionYear"

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
            headers={"X-Emby-Token": self._token, "Accept": "application/json"},
            verify=self._verify,
            timeout=self._timeout,
        )

    async def _items(
        self, client: httpx.AsyncClient, *, start: int, limit: int
    ) -> dict:
        try:
            response = await client.get(
                self.ITEMS_PATH,
                params={
                    "IncludeItemTypes": "Movie",
                    "Recursive": "true",
                    "Fields": self.FIELDS,
                    "EnableImages": "false",
                    "EnableUserData": "false",
                    "StartIndex": start,
                    "Limit": limit,
                },
            )
        except httpx.HTTPError as exc:
            raise JellyfinError(f"could not reach Jellyfin: {exc}") from exc
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise JellyfinAuthError("Jellyfin rejected the API key")
        if response.is_error:
            raise JellyfinError(f"Jellyfin answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise JellyfinError("Jellyfin answered something that is not JSON") from exc

    async def movie_section_keys(self) -> list[str]:
        """Whether there is a movie library to read, in the shape the Plex client
        answers it — one call, `Limit=0`, so the Test probe costs a count and no items.

        A list rather than a bool because the caller is shared: "reachable and
        authenticated but nothing to read" is a failed test on either server.
        """
        async with self._client() as client:
            document = await self._items(client, start=0, limit=0)
        return ["movies"] if int(document.get("TotalRecordCount") or 0) > 0 else []

    async def list_movies(self) -> MediaServerFetch:
        """Every movie, paginated, capped, and honest about it."""
        movies: list[MediaServerMovie] = []
        truncated = False
        async with self._client() as client:
            start = 0
            pages = 0
            while True:
                if pages >= MAX_LIBRARY_PAGES:
                    truncated = True
                    break
                document = await self._items(
                    client, start=start, limit=JELLYFIN_PAGE_SIZE
                )
                items = document.get("Items") or []
                movies.extend(_movie_from_jellyfin_item(item) for item in items)
                pages += 1
                start += len(items)
                total = int(document.get("TotalRecordCount") or 0)
                if not items or start >= total:
                    break
        return MediaServerFetch(movies=tuple(movies), truncated=truncated)


def _movie_from_jellyfin_item(item: dict[str, Any]) -> MediaServerMovie:
    """One Jellyfin item reduced to ids and identity.

    Ids are read by KEY EQUALITY, never by searching the text. A live library answers
    with `{"Imdb": ..., "Tmdb": ..., "TmdbCollection": ...}`, and the Plex client's
    approach — regex over the joined guid text — would match the COLLECTION id and
    stamp it on the film. That is the wrong-poster failure M5 exists to prevent,
    arriving from a new direction, so it is closed here before it can happen.
    """
    provider_ids = item.get("ProviderIds")
    by_key = {
        str(key).strip().casefold(): str(value)
        for key, value in (provider_ids or {}).items()
        if isinstance(key, str)
    }
    tmdb_raw = by_key.get("tmdb", "")
    imdb_raw = by_key.get("imdb", "")
    year = item.get("ProductionYear")
    return MediaServerMovie(
        title=str(item.get("Name", "")),
        year=int(year) if isinstance(year, int) else None,
        tmdb_id=int(tmdb_raw) if tmdb_raw.isdigit() else None,
        # Bounded exactly as the Plex side is: an id outside the range IMDb issues is
        # no id, never a truncated one.
        imdb_id=imdb_raw if _IMDB_ID_RE.fullmatch(imdb_raw) else None,
    )


def _movie_from_item(item: dict[str, Any]) -> MediaServerMovie:
    """One Plex item reduced to ids and identity, whichever agent catalogued it."""
    guid_texts = [str(item.get("guid", ""))]
    guid_texts += [
        str(child.get("id", "")) for child in item.get("Guid", []) if isinstance(child, dict)
    ]
    joined = " ".join(guid_texts)
    tmdb_match = _TMDB_GUID_RE.search(joined)
    imdb_match = _IMDB_GUID_RE.search(joined)
    year = item.get("year")
    return MediaServerMovie(
        title=str(item.get("title", "")),
        year=int(year) if isinstance(year, int) else None,
        tmdb_id=int(tmdb_match.group(1)) if tmdb_match else None,
        imdb_id=imdb_match.group(1) if imdb_match else None,
    )


@dataclass(frozen=True)
class MediaServerSnapshot:
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


def snapshot_from_movies(
    movies: tuple[MediaServerMovie, ...], *, truncated: bool = False
) -> MediaServerSnapshot:
    title_years: dict[str, set[int | None]] = {}
    for movie in movies:
        if movie.title:
            title_years.setdefault(normalize_title(movie.title), set()).add(movie.year)
    return MediaServerSnapshot(
        tmdb_ids=frozenset(m.tmdb_id for m in movies if m.tmdb_id is not None),
        imdb_ids=frozenset(m.imdb_id for m in movies if m.imdb_id is not None),
        title_years={title: frozenset(years) for title, years in title_years.items()},
        truncated=truncated,
    )


class MediaServerLibraryCache:
    """The last fetched library on disk, so renders inside the TTL cost Plex nothing.

    A cache, not a record: unreadable, hand-edited or written by a newer build all read
    as empty, because losing it costs one refetch and refusing to render costs the page.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / LIBRARY_CACHE_FILENAME

    def save(self, fetch: MediaServerFetch) -> None:
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
            schema_version=LIBRARY_CACHE_SCHEMA_VERSION,
        )

    def load(self) -> tuple[MediaServerSnapshot, float] | None:
        """The cached snapshot and when it was fetched, or None when there is none.

        Age is the CALLER's decision: the render path wants a fresh one but will take
        stale over nothing when Plex is down, and the Refresh button wants none at all.
        """
        if not self._path.exists():
            return None
        try:
            document = filestore.read_json(
                self._path, expected_version=LIBRARY_CACHE_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            return None
        stored = document.get(MOVIES_KEY)
        if not isinstance(stored, list):
            return None
        movies = tuple(
            MediaServerMovie(
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
