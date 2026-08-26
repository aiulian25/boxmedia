"""Discovery credentials — the TMDB API key and the Trakt client ID (TV step 5).

The app ships NO keys of its own. These are the user's: entered in Settings, changed or
deleted whenever they like, and encrypted at rest with the same AES-256-GCM field
encryption the Radarr and Sonarr API keys use. Nothing here returns a decrypted value
except the explicit `decrypt_*` accessors the clients and the Test buttons call.

The Trakt client ID is, strictly, a public identifier — it ships in the headers of every
request and identifies the application rather than the person. It is stored encrypted
anyway: one pattern for every credential-shaped thing in this app is worth more than the
handful of bytes saved by special-casing it, and a reader should never have to remember
which secrets in `config/` are real.

## The query-parameter deviation, and what pays for it

Every other credential in BoxMedia travels in a header, never a URL — a URL reaches proxy
logs, browser history and exception traces. TMDB's v3 API does not offer that: the key
is a query parameter, and that is the contract, not a choice we get to make.

So the deviation is contained rather than argued with. `redact` below strips any
`api_key=` value out of a string, and it is applied to every message this module builds
from a URL or an httpx error before that message reaches an exception, a log line or a
page. Steps 6 and 7 use the same helper at their own call sites. The rule is: no string
derived from a TMDB request leaves this package without going through `redact` first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app import __version__
from app.core import crypto, filestore
from app.core.audit import AuditAction, AuditLog

DISCOVERY_SCHEMA_VERSION = 1
DISCOVERY_FILENAME = "discovery.yml"

TMDB_KEY_FIELD = "tmdb_key_encrypted"
TRAKT_KEY_FIELD = "trakt_client_id_encrypted"

# Which credential a route is talking about. Closed set, like every other kind in this
# app: the form submits one of two and anything else is refused rather than guessed.
PROVIDER_TMDB = "tmdb"
PROVIDER_TRAKT = "trakt"
PROVIDERS = (PROVIDER_TMDB, PROVIDER_TRAKT)
# What each is called where a person reads it, and which stored field it writes.
PROVIDER_NAMES = {PROVIDER_TMDB: "TMDB", PROVIDER_TRAKT: "Trakt"}
PROVIDER_FIELDS = {PROVIDER_TMDB: TMDB_KEY_FIELD, PROVIDER_TRAKT: TRAKT_KEY_FIELD}

# The two public APIs. Constants rather than literals at the call sites: steps 6 and 7
# import these, and one base URL to change beats three.
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
TRAKT_BASE_URL = "https://api.trakt.tv"
TRAKT_API_VERSION = "2"
# Ours, not another application's, and naming the build that is actually running. A
# public API is entitled to know who is calling — the teardown found nzb360 announcing
# itself as "nzb360/1.0" to Trakt, and borrowing someone else's identity is both rude
# and useless to the operator trying to work out who is generating traffic.
USER_AGENT = f"BoxMedia/{__version__} (+https://github.com/aiulian25/boxmedia)"

PROBE_TIMEOUT_SECONDS = 4.0

# The same mask the connection cards show. Mirrored rather than imported so a credential
# store does not depend on the connection store for a UI string; a test pins the two
# equal so they cannot drift — the deliberate mirror backup.py already documents for
# MIRRORED_FILTERS_FILENAME.
KEY_MASK = "••••••••••••"

# Anything shaped like TMDB's key parameter, whatever the value. Value-blind on purpose:
# a redactor that only knew the CURRENT key would leak a rotated one, and would leak a
# key typed into the Test box before it was ever stored.
_API_KEY_PARAM_RE = re.compile(r"(api_key=)[^&\s\"'>]*", re.IGNORECASE)
REDACTED = "REDACTED"


def redact(text: object) -> str:
    """Strip credential values out of anything on its way to a log, page or exception.

    Applied to every message this package builds from a URL or an httpx error. TMDB's key
    rides in the query string because their API says so (see the module docstring), and
    httpx puts the full URL into the text of most of its exceptions — so without this a
    single unreachable host writes the user's key into the audit log.
    """
    return _API_KEY_PARAM_RE.sub(rf"\1{REDACTED}", str(text))


def scrub(exc: BaseException) -> BaseException:
    """Neutralise a caught exception before raising our own in its place.

    `raise OurError(...) from None` suppresses the original for a printed traceback, but
    it does NOT clear `__context__` — the original object is still attached, and anything
    that walks the chain rather than formatting a traceback (a structured logger, an
    error reporter, a debugger repr) reads it. For an httpx transport error that object's
    text is the request URL, which for TMDB carries the key.

    So the original is scrubbed in place: its own message is redacted, and the `Request`
    httpx hangs off it — whose `.url` carries the key structurally, not just as text — is
    dropped. The exception is on its way to being discarded either way; this makes it
    safe to discard sloppily.
    """
    exc.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in exc.args)
    try:
        # httpx stores it privately and exposes `.request`, which then raises rather
        # than answering. That is the right failure for an object nothing should read.
        exc._request = None  # noqa: SLF001 — deliberately reaching into a doomed object
    except AttributeError:
        pass
    return exc


class DiscoveryError(Exception):
    """Base class for discovery-credential failures."""


@dataclass(frozen=True)
class DiscoveryKeys:
    """What is configured, said without revealing anything.

    Deliberately carries no plaintext: this is what the Settings page renders, and a
    template that cannot reach a secret cannot leak one.
    """

    has_tmdb: bool
    has_trakt: bool

    @property
    def ready(self) -> bool:
        """Both present — the state Discover needs before it can show anything."""
        return self.has_tmdb and self.has_trakt

    def public(self) -> dict[str, object]:
        return {
            "has_tmdb": self.has_tmdb,
            "has_trakt": self.has_trakt,
            "ready": self.ready,
            "mask": KEY_MASK,
        }


class DiscoveryStore:
    """The TMDB key and Trakt client ID, encrypted at rest.

    One record, `mediaserver.yml`-style. Blank keeps the stored value, so editing one
    credential never forces re-pasting the other; Delete is explicit and separate,
    because "clear this" and "leave it alone" must not be the same gesture on a form.
    """

    def __init__(self, config_dir: Path, *, key: bytes, audit: AuditLog | None = None) -> None:
        self._path = config_dir / DISCOVERY_FILENAME
        self._key = key
        self._audit = audit

    def _load_raw(self) -> dict:
        if not self._path.exists():
            return {}
        document = filestore.read_yaml(self._path, expected_version=DISCOVERY_SCHEMA_VERSION)
        document.pop(filestore.SCHEMA_VERSION_KEY, None)
        return document

    def _save_raw(self, document: dict) -> None:
        filestore.write_yaml(
            self._path, document, schema_version=DISCOVERY_SCHEMA_VERSION
        )

    def load(self) -> DiscoveryKeys:
        """Which credentials exist. Never their values."""
        stored = self._load_raw()
        return DiscoveryKeys(
            has_tmdb=bool(stored.get(TMDB_KEY_FIELD)),
            has_trakt=bool(stored.get(TRAKT_KEY_FIELD)),
        )

    def save(self, provider: str, secret: str) -> None:
        """Store one credential. A blank value is a no-op, not an erase.

        Blank-keeps is the contract every other secret field in this app has: it is what
        lets someone re-save the card without re-pasting what is already there. Erasing
        is `remove`, which the card offers as its own button.
        """
        field = self._field_for(provider)
        cleaned = secret.strip()
        if not cleaned or cleaned == KEY_MASK:
            return
        stored = self._load_raw()
        stored[field] = crypto.encrypt_field(cleaned, self._key)
        self._save_raw(stored)
        if self._audit:
            # The action and which provider — never the value, and never its length.
            self._audit.record(AuditAction.DISCOVERY_KEY_SAVED, provider=provider)

    def remove(self, provider: str) -> bool:
        """Forget one credential. True when there was one to forget."""
        field = self._field_for(provider)
        stored = self._load_raw()
        if stored.pop(field, None) is None:
            return False
        self._save_raw(stored)
        if self._audit:
            self._audit.record(AuditAction.DISCOVERY_KEY_REMOVED, provider=provider)
        return True

    def decrypt(self, provider: str) -> str | None:
        """One credential in plaintext, or None when none is stored.

        The only path out of this store that yields a value, and it exists for exactly
        two callers: the clients that must send it, and the Test buttons.
        """
        stored = self._load_raw().get(self._field_for(provider))
        if not stored:
            return None
        return crypto.decrypt_field(str(stored), self._key)

    def tmdb_key(self) -> str | None:
        return self.decrypt(PROVIDER_TMDB)

    def trakt_client_id(self) -> str | None:
        return self.decrypt(PROVIDER_TRAKT)

    @staticmethod
    def _field_for(provider: str) -> str:
        field = PROVIDER_FIELDS.get(provider)
        if field is None:
            raise DiscoveryError(f"unknown discovery provider: {provider!r}")
        return field


class ProbeResult:
    """Why a credential test ended the way it did.

    The same three states the connection cards use, so one fragment renders all of them
    and a green line means the same thing everywhere in Settings.
    """

    OK = "ok"
    AUTH = "auth"
    UNREACHABLE = "unreachable"


async def probe_tmdb(api_key: str, *, verify: bool | str = True) -> str:
    """Does this TMDB key work? `GET /configuration` — the cheapest authenticated call.

    Errors are redacted before they can become an exception message: httpx puts the
    request URL into most of its error text, and for TMDB that URL carries the key.
    """
    return await _probe(
        f"{TMDB_BASE_URL}/configuration",
        params={"api_key": api_key.strip()},
        headers={"User-Agent": USER_AGENT},
        verify=verify,
    )


def trakt_headers(client_id: str) -> dict[str, str]:
    """What every Trakt request carries. One builder so the credential probe and the
    real client can never disagree about what a Trakt request looks like.

    The client ID is a HEADER, never a query parameter — which is why nothing on the
    Trakt side needs `redact`: its URLs carry no credential at all.
    """
    return {
        "trakt-api-key": client_id.strip(),
        "trakt-api-version": TRAKT_API_VERSION,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


async def probe_trakt(client_id: str, *, verify: bool | str = True) -> str:
    """Does this Trakt client ID work? One trending row is the smallest thing to ask for."""
    return await _probe(
        f"{TRAKT_BASE_URL}/shows/trending",
        params={"limit": 1},
        headers=trakt_headers(client_id),
        verify=verify,
    )


async def _probe(
    url: str, *, params: dict, headers: dict, verify: bool | str
) -> str:
    """One request, one verdict. Never raises — a probe reports, it does not fail.

    TLS is verified with whatever the app's outbound settings resolve to, exactly like
    every other outbound call; these are public APIs on real certificates, so there is no
    legitimate reason for that to be off, but the setting is the app's and not this
    module's to override.
    """
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, verify=verify) as client:
            response = await client.get(url, params=params, headers=headers)
    except (httpx.HTTPError, OSError):
        # The exception is dropped rather than wrapped: its text carries the request URL,
        # and for TMDB that URL carries the key. There is nothing in it a person needs
        # that "could not reach it" does not already say.
        return ProbeResult.UNREACHABLE
    if response.status_code in (401, 403):
        return ProbeResult.AUTH
    if response.status_code >= 400:
        return ProbeResult.UNREACHABLE
    return ProbeResult.OK
