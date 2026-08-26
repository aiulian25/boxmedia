"""Async Trakt client (TV step 7) — the ranking half of Discover.

Two endpoints, and that is genuinely all: trending, and anticipated. The teardown
(`tv-discovery.md` §5) found nzb360 using exactly those two plus comments, and comments
are not something this app shows. A client that stays this small is a client nobody has
to audit twice.

Trakt answers a different question from TMDB, and the difference is the point of having
both. Trakt says **what people are watching right now**; TMDB says what a show looks
like and what its ids are. Discover joins them.

## Why a Trakt row is worth more than a TMDB one

A trending entry carries all four ids — trakt, imdb, tmdb and **tvdb** — in the same
payload as the title. That is the whole reason this module exists rather than ranking by
TMDB's own popularity: a Trakt row can be added to Sonarr immediately, while a
TMDB-sourced row has to be bridged through `tmdb.tvdb_id_for` first (§6). Free ids are
the difference between an Add that works and an Add that needs another round trip.

## What this client is NOT

No account, no OAuth, no device pairing, no token storage — ruling 2, and what the
teardown found in nzb360 too. Trending here is GLOBAL trending, not anybody's watchlist,
and nothing is ever written to Trakt. The client ID identifies the *application*, travels
in a header, and is the only credential involved: there is nothing else to leak, and
unlike the TMDB side no URL ever carries it, so nothing here needs `discovery.redact`.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.services.discovery import TRAKT_BASE_URL, scrub, trakt_headers

REQUEST_TIMEOUT_SECONDS = 10.0
# One page is what a Discover row shows. Trakt pages at 10 by default, which would leave
# a six-card row looking thin the moment anything is filtered out of it.
DEFAULT_LIMIT = 20
# Trakt caps a page at 100. Asking for more is not an error there, it is silently
# ignored — so the bound is applied here, where it can be seen.
MAX_LIMIT = 100


class TraktError(Exception):
    """Base class for all Trakt client failures."""


class TraktAuthError(TraktError):
    """Trakt rejected the client ID (HTTP 401/403).

    Its own type so the Settings Test button can say "Trakt rejected the client ID"
    rather than "could not reach it" — two different things to go and fix.
    """


class TraktConnectionError(TraktError):
    """Could not reach Trakt, or its TLS certificate failed validation."""


@dataclass(frozen=True)
class TraktShow:
    """One ranked show.

    The four ids are the payload that matters. `tvdb_id` is what Sonarr keys on, and a
    Trakt row hands it over for free — see the module docstring.

    The two counts are deliberately separate rather than one "score". They are not the
    same measurement: `watchers` is how many people are watching this minute, and
    `list_count` is how many people have put it on a list to watch later. Collapsing them
    would make a row claim a number that means whatever the caller assumed.
    """

    title: str
    year: int | None
    trakt_id: int | None
    slug: str | None
    imdb_id: str | None
    tmdb_id: int | None
    tvdb_id: int | None
    overview: str | None = None
    watchers: int | None = None
    list_count: int | None = None

    @property
    def addable(self) -> bool:
        """Whether Sonarr could take this show without a bridging call."""
        return self.tvdb_id is not None


def _int_or_none(value: object) -> int | None:
    """A real integer, or None. `bool` is an int in Python and would become 0/1."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _show_from(entry: dict, *, watchers_key: str | None) -> TraktShow | None:
    """One `{count, show: {...}}` row -> TraktShow, or None if it is not one.

    Both endpoints wrap the show the same way and differ only in what they call the
    number beside it, so one reader serves both and they cannot drift in what they read
    — `radarr._library_movie`'s reason.
    """
    show = entry.get("show")
    if not isinstance(show, dict):
        return None
    title = _text_or_none(show.get("title"))
    if title is None:
        # A row with no title is a row nothing can render or match on.
        return None
    ids = show.get("ids")
    ids = ids if isinstance(ids, dict) else {}
    count = _int_or_none(entry.get(watchers_key)) if watchers_key else None
    return TraktShow(
        title=title,
        year=_int_or_none(show.get("year")),
        trakt_id=_int_or_none(ids.get("trakt")),
        slug=_text_or_none(ids.get("slug")),
        imdb_id=_text_or_none(ids.get("imdb")),
        tmdb_id=_int_or_none(ids.get("tmdb")),
        tvdb_id=_int_or_none(ids.get("tvdb")),
        overview=_text_or_none(show.get("overview")),
        watchers=count if watchers_key == "watchers" else None,
        list_count=count if watchers_key == "list_count" else None,
    )


class TraktClient:
    def __init__(
        self,
        client_id: str,
        *,
        verify: bool | str = True,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._client_id = client_id.strip()
        self._verify = verify
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=TRAKT_BASE_URL,
            timeout=self._timeout,
            verify=self._verify,
            # Built by discovery.trakt_headers so this and the Settings credential probe
            # can never disagree about what a Trakt request looks like.
            headers=trakt_headers(self._client_id),
        )

    async def _request(self, path: str, params: dict) -> object:
        try:
            async with self._client() as client:
                response = await client.get(path, params=params)
        except (httpx.ConnectError, OSError) as exc:
            # OSError covers ssl.SSLError and a misconfigured CA path raised while
            # building the SSL context — degrade to "unreachable" rather than 500-ing
            # every page that touches Trakt.
            raise TraktConnectionError(f"cannot reach Trakt: {scrub(exc)}") from None
        except httpx.HTTPError as exc:
            raise TraktConnectionError(f"request to Trakt failed: {scrub(exc)}") from None

        if response.status_code in (401, 403):
            raise TraktAuthError("Trakt rejected the client ID")
        if response.status_code >= 400:
            raise TraktError(f"Trakt returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise TraktError("Trakt returned a non-JSON response") from exc

    async def _ranked(
        self, path: str, *, limit: int, watchers_key: str, extended: bool
    ) -> list[TraktShow]:
        rows = await self._request(
            path,
            {
                "limit": max(1, min(limit, MAX_LIMIT)),
                "page": 1,
                # `extended=full` costs a bigger body but brings the overview and the
                # year with it. Only trending asks for it: an anticipated show often has
                # no overview written yet, so the extra bytes buy nothing.
                **({"extended": "full"} if extended else {}),
            },
        )
        if not isinstance(rows, list):
            # A bare `for item in ...` over a dict silently yields its KEYS, so a shape
            # surprise would become wrong data rather than a caught error.
            raise TraktError("unexpected Trakt response shape")
        shows = (
            _show_from(row, watchers_key=watchers_key)
            for row in rows
            if isinstance(row, dict)
        )
        return [show for show in shows if show is not None]

    async def trending_shows(self, limit: int = DEFAULT_LIMIT) -> list[TraktShow]:
        """What people are watching right now, most-watched first.

        `watchers` is the ranking, and it is the honest one to show beside a card: it is
        a count of people, not a score somebody computed. Trakt returns them in order,
        which this preserves.
        """
        return await self._ranked(
            "/shows/trending", limit=limit, watchers_key="watchers", extended=True
        )

    async def anticipated_shows(self, limit: int = DEFAULT_LIMIT) -> list[TraktShow]:
        """Shows that have not aired yet, most-listed first.

        `list_count` is a different measurement from `watchers` — how many people are
        waiting, not how many are watching — which is why the two live in separate
        fields and the row that renders them says which it is showing.
        """
        return await self._ranked(
            "/shows/anticipated", limit=limit, watchers_key="list_count", extended=False
        )
