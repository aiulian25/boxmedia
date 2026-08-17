"""HTTP hardening: security headers, same-origin defense, login rate-limiting.

Applied once here so it covers every route. The lockout matters most of anything
generic for this app: a single internet-exposed admin account is the textbook
brute-force target. The strict CSP is affordable because the app ships no inline
scripts/styles and serves posters locally (`img-src 'self'`, Step 16).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

CSP = (
    "default-src 'self'; "
    "img-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
HSTS = "max-age=63072000; includeSubDomains"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_LOCK_SECONDS = 900
# Opportunistically garbage-collect expired limiter entries once a map grows past this,
# so distinct client IPs can't accumulate unboundedly in a long-lived process.
MAX_TRACKED_KEYS = 1000


def _is_https(request: Request) -> bool:
    # request.url.scheme reflects X-Forwarded-Proto when uvicorn runs --proxy-headers.
    return request.url.scheme == "https"


def _acceptable_hosts(request: Request) -> set[str]:
    """Hosts a same-origin request may legitimately carry.

    Behind a reverse proxy (BoxMedia's intended deployment) the browser's Origin is
    the PUBLIC host, while the container's own Host header is the backend address.
    Proxies convey the original host via X-Forwarded-Host, so we accept that too.
    Spoofing X-Forwarded-Host does not help a CSRF attacker: the SameSite=Strict
    session cookie is never sent cross-site, so this stays defense-in-depth.
    """
    hosts: set[str] = set()
    direct = request.headers.get("host")
    if direct:
        hosts.add(direct)
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        for candidate in forwarded.split(","):
            trimmed = candidate.strip()
            if trimmed:
                hosts.add(trimmed)
    return hosts


def _origin_matches_host(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        # A missing Origin, or an opaque "null" Origin (browsers send "null" for
        # sandboxed/privacy contexts), can't be matched to a host. The SameSite=Strict
        # session cookie is the primary CSRF defense — a real cross-site POST never
        # carries the cookie — so allow these and rely on that. A present, concrete
        # Origin (e.g. https://evil.example) is still required to match.
        return True
    return urlparse(origin).netloc in _acceptable_hosts(request)


def csrf_token_for(session_secret: str, session_id: str) -> str:
    """A per-session CSRF token: HMAC-SHA256 of the session id under the app secret.

    Derived, not stored — no server-side token table to keep in sync with sessions, and
    the value changes with every new session. This is what `BM_SESSION_SECRET` is for.
    """
    return hmac.new(
        session_secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def csrf_valid(session_secret: str, session_id: str | None, supplied: str | None) -> bool:
    """Constant-time check of a submitted token against the session's expected one."""
    if not session_id or not supplied:
        return False
    return hmac.compare_digest(csrf_token_for(session_secret, session_id), supplied)


def install(app: FastAPI) -> None:
    """Register the hardening middleware. Runs outermost (added after the gate)."""

    @app.middleware("http")
    async def harden(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in MUTATING_METHODS and not _origin_matches_host(request):
            return PlainTextResponse("cross-origin request rejected", status_code=403)

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if _is_https(request):
            response.headers["Strict-Transport-Security"] = HSTS
        return response


class LoginRateLimiter:
    """In-memory fixed-window limiter with lockout, keyed by client IP.

    ponytail: in-memory + single-process. Behind multiple workers each has its own
    counters; move to a shared /data-backed store only if BoxMedia ever runs
    multi-worker (it does not by default).
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        lock_seconds: int = DEFAULT_LOCK_SECONDS,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lock_seconds = lock_seconds
        self._failures: dict[str, tuple[int, float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        return time.monotonic()

    def is_locked(self, key: str) -> bool:
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return False
            if until <= self._now():
                del self._locked_until[key]
                return False
            return True

    def record_failure(self, key: str) -> bool:
        """Record a failed attempt. Returns True if the key is now locked out."""
        with self._lock:
            now = self._now()
            # Opportunistic GC: entries are otherwise only dropped when their own key is
            # re-read, so distinct IPs would pile up forever. Purge expired ones whenever
            # a map outgrows MAX_TRACKED_KEYS.
            if len(self._failures) > MAX_TRACKED_KEYS:
                self._failures = {
                    tracked_key: value
                    for tracked_key, value in self._failures.items()
                    if now - value[1] <= self._window_seconds
                }
            if len(self._locked_until) > MAX_TRACKED_KEYS:
                self._locked_until = {
                    tracked_key: until
                    for tracked_key, until in self._locked_until.items()
                    if until > now
                }
            count, window_start = self._failures.get(key, (0, now))
            if now - window_start > self._window_seconds:
                count, window_start = 0, now
            count += 1
            self._failures[key] = (count, window_start)
            if count >= self._max_attempts:
                self._locked_until[key] = now + self._lock_seconds
                self._failures.pop(key, None)
                return True
            return False

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
