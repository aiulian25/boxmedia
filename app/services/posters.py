"""Local poster cache (Step 16).

Posters are fetched once from Radarr's metadata URLs and served from
`/data/cache/posters` so the dashboard's images are same-origin — which lets the
Content-Security-Policy (Step 21) stay `img-src 'self'` with no third-party image
hosts allow-listed, and keeps the grid intact when a remote URL later rots.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path

import httpx

from app.core.filestore import atomic_write_bytes, dir_size_bytes

POSTER_SUBDIR = "posters"
POSTER_SUFFIX = ".jpg"
_POSTER_NAME_RE = re.compile(r"^[0-9a-f]{40}\.jpg$")
_DOWNLOAD_TIMEOUT_SECONDS = 8.0
MAX_POSTER_BYTES = 5 * 1024 * 1024  # skip an oversized/garbage URL rather than fill disk
# How long a failed URL is left alone before it is worth another try. Long enough that an
# image host being down costs one attempt per page-load burst instead of one per poster
# per view; short enough that a passing blip does not cost the poster until a restart.
FAILED_RETRY_AFTER_SECONDS = 300.0

# Radarr hands out TMDB's `original`, which for a poster is ~1 MB and up to 3 MB. A page
# of 32 of those is ~35 MB that paints in visible progressive-decode passes. TMDB serves
# sized variants from the same path, so ask for one that fits the box instead.
_TMDB_IMAGE_PATH = "/t/p/"
# Grid posters render at 208x312 CSS px. Handing the browser a 2000px image means a 9.6x
# downscale, and a GPU rasterizer does that with plain bilinear filtering — no mipmaps —
# which stair-steps poster lettering and drops pixels out of thin strokes. 500 keeps the
# ratio at 2.4x on a 1x display and 1.2x on a 2x one, so detail survives at either. The
# movie modal reuses this URL, so the grid and the modal share one cache entry.
POSTER_WIDTH = "w500"
# Credit headshots render at 96x144 CSS px — 185 is ~native on a 2x display.
HEADSHOT_WIDTH = "w185"


def sized(url: str | None, width: str) -> str | None:
    """A TMDB image URL asking for `width` instead of whatever size it names.

    Anything that is not a TMDB image URL is returned untouched, so a Radarr-hosted or
    third-party poster still works. Every caller MUST route through this — the cache keys
    on the URL, so caching one form and pruning another would delete every poster.
    """
    if not url or _TMDB_IMAGE_PATH not in url:
        return url
    prefix, _, remainder = url.partition(_TMDB_IMAGE_PATH)
    current_size, separator, path = remainder.partition("/")
    if not separator or not current_size:
        return url
    return f"{prefix}{_TMDB_IMAGE_PATH}{width}/{path}"


class PosterCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir / POSTER_SUBDIR
        # When each URL last failed, so a dead image host is not re-tried on every page
        # view. `cache_posters` awaits these before rendering, so without it an outage
        # adds the full download timeout to every poster-bearing page.
        # ponytail: unbounded per-process dict — bounded in practice by the distinct
        # poster URLs across retained reports (MAX_REPORTS x chart_size); add an LRU only
        # if that ever stops being true.
        self._failed_at: dict[str, float] = {}

    def _name(self, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()  # noqa: S324 — cache key, not security
        return f"{digest}{POSTER_SUFFIX}"

    def local_name(self, url: str) -> str:
        return self._name(url)

    def is_cached(self, url: str) -> bool:
        return (self._dir / self._name(url)).exists()

    async def ensure(self, client: httpx.AsyncClient, url: str) -> bool:
        """Download the poster if not already cached. Best-effort; never raises."""
        target = self._dir / self._name(url)
        if target.exists():
            return True
        if self._failed_recently(url):
            return False
        try:
            # A hard wall-clock bound on top of httpx's timeouts, which are per-operation:
            # a host trickling one byte per read window resets them forever. Same reason
            # `app.web.deps` wraps its Radarr calls.
            payload = await asyncio.wait_for(
                self._download(client, url), timeout=_DOWNLOAD_TIMEOUT_SECONDS
            )
        except (httpx.HTTPError, TimeoutError):
            payload = None
        if payload is None:
            # Unreachable, refused, or past the size cap — all of them mean "do not ask
            # this URL again for a while".
            self._failed_at[url] = time.monotonic()
            return False
        # Atomic write (temp + fsync + rename) so an interrupted download never leaves a
        # torn JPEG that would then be served — and backed up — forever.
        atomic_write_bytes(target, payload)
        return True

    async def _download(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        """The poster body, or None once it runs past MAX_POSTER_BYTES.

        Measured as it arrives rather than after `response.content` has read the whole
        body: checking the length afterwards caps the DISK, not the memory it took to get
        there, and this runs in a 256 MB container.
        """
        async with client.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            received = bytearray()
            async for chunk in response.aiter_bytes():
                received.extend(chunk)
                if len(received) > MAX_POSTER_BYTES:
                    return None
            return bytes(received)

    def _failed_recently(self, url: str) -> bool:
        """True while a recent failure should still be honoured, expiring the entry once
        it is old enough to be worth another attempt."""
        failed_at = self._failed_at.get(url)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at < FAILED_RETRY_AFTER_SECONDS:
            return True
        del self._failed_at[url]
        return False

    def prune(self, keep_urls: set[str]) -> int:
        """Delete cached posters no retained report references. Returns the count removed.

        Only files matching this cache's own naming scheme are touched — anything else an
        operator has put in the directory is not ours to unlink. `keep_urls` is derived
        from the stored reports by the caller, never from a request.
        """
        if not self._dir.exists():
            return 0
        keep_names = {self._name(url) for url in keep_urls}
        removed = 0
        for path in self._dir.glob(f"*{POSTER_SUFFIX}"):
            if path.name in keep_names or not _POSTER_NAME_RE.match(path.name):
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def size_bytes(self) -> int:
        return dir_size_bytes(self._dir)

    def serve_path(self, name: str) -> Path | None:
        if not _POSTER_NAME_RE.match(name):
            return None
        candidate = self._dir / name
        return candidate if candidate.exists() else None
