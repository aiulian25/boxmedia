"""Poster cache hardening (Steps 5 + 18): size cap, atomic write, serve_path guard."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import httpx
import respx

from app.services import posters
from app.services.posters import (
    FAILED_RETRY_AFTER_SECONDS,
    MAX_POSTER_BYTES,
    POSTER_SUBDIR,
    POSTER_SUFFIX,
    POSTER_WIDTH,
    SERIES_POSTER_WIDTH,
    PosterCache,
    sized,
)
from app.services.tmdb import image_url

POSTER_URL = "http://radarr.local/MediaCover/1/poster.jpg"


@respx.mock
async def test_ensure_caches_a_normal_poster(tmp_path: Path) -> None:
    respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg"))
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is True
    assert cache.is_cached(POSTER_URL)


@respx.mock
async def test_ensure_rejects_oversized_poster(tmp_path: Path) -> None:
    # One bad metadata URL must not fill the disk or cache a giant blob.
    respx.get(POSTER_URL).mock(
        return_value=httpx.Response(200, content=b"x" * (MAX_POSTER_BYTES + 1))
    )
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is False
    assert not cache.is_cached(POSTER_URL)  # nothing was written


@respx.mock
async def test_ensure_leaves_no_partial_file_on_http_error(tmp_path: Path) -> None:
    respx.get(POSTER_URL).mock(return_value=httpx.Response(500))
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is False
    assert not cache.is_cached(POSTER_URL)


def test_serve_path_rejects_unsafe_and_malformed_names(tmp_path: Path) -> None:
    # /posters/{name} serves user-supplied names; only a hashed cache filename is valid,
    # so traversal and arbitrary paths must be refused (return None).
    cache = PosterCache(tmp_path)
    assert cache.serve_path("../evil.jpg") is None
    assert cache.serve_path("../../etc/passwd") is None
    assert cache.serve_path("not-a-40-hex-name.jpg") is None
    assert cache.serve_path("abcd.png") is None  # wrong suffix
    assert cache.serve_path("0" * 40 + ".jpg") is None  # valid format but no such file


def test_serve_path_returns_existing_cached_file(tmp_path: Path) -> None:
    cache = PosterCache(tmp_path)
    name = cache.local_name("http://img/poster.jpg")  # 40-hex + .jpg
    target = tmp_path / POSTER_SUBDIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff jpg")
    assert cache.serve_path(name) == target


OTHER_POSTER_URL = "http://radarr.local/MediaCover/2/poster.jpg"


def _seed(cache_dir: Path, url: str, cache: PosterCache) -> Path:
    path = cache_dir / POSTER_SUBDIR / cache.local_name(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff jpeg")
    return path


def test_prune_keeps_referenced_and_removes_orphans(tmp_path: Path) -> None:
    cache = PosterCache(tmp_path)
    kept = _seed(tmp_path, POSTER_URL, cache)
    orphan = _seed(tmp_path, OTHER_POSTER_URL, cache)

    assert cache.prune({POSTER_URL}) == 1
    assert kept.exists()
    assert not orphan.exists()


def test_prune_leaves_files_this_cache_did_not_write(tmp_path: Path) -> None:
    # A delete button must only remove what BoxMedia itself created.
    cache = PosterCache(tmp_path)
    _seed(tmp_path, POSTER_URL, cache)
    stray = tmp_path / POSTER_SUBDIR / "holiday-photo.jpg"
    stray.write_bytes(b"not ours")

    assert cache.prune(set()) == 1  # only the real cache entry
    assert stray.exists()


def test_prune_on_a_missing_directory_is_a_noop(tmp_path: Path) -> None:
    assert PosterCache(tmp_path).prune(set()) == 0


def test_size_bytes_sums_the_cache(tmp_path: Path) -> None:
    cache = PosterCache(tmp_path)
    assert cache.size_bytes() == 0  # nothing written yet
    _seed(tmp_path, POSTER_URL, cache)
    _seed(tmp_path, OTHER_POSTER_URL, cache)
    assert cache.size_bytes() == 2 * len(b"\xff\xd8\xff jpeg")


# --- streaming cap + negative cache (review step 7) ---

ONE_MEGABYTE = b"x" * (1024 * 1024)
POSTER_URL = "http://images.example/poster.jpg"


def _endless_megabytes(counter: dict[str, int]):  # noqa: ANN202
    """A body that never ends — the cap is the only thing that can stop it.

    A plain oversized `content=` is served by respx as ONE chunk, so it would not prove
    the read stops early. This does: if the cap is checked after the body is buffered,
    the test hangs instead of failing politely.
    """

    async def stream():  # noqa: ANN202
        while True:
            counter["megabytes"] += 1
            yield ONE_MEGABYTE

    return stream()


@respx.mock
async def test_an_endless_body_is_cut_off_at_the_cap(tmp_path: Path) -> None:
    served = {"megabytes": 0}
    respx.get(POSTER_URL).mock(
        return_value=httpx.Response(200, content=_endless_megabytes(served))
    )
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is False

    assert not cache.is_cached(POSTER_URL)
    # Read only far enough to know it was too big, not the whole (infinite) body.
    assert served["megabytes"] <= (MAX_POSTER_BYTES // len(ONE_MEGABYTE)) + 2


@respx.mock
async def test_a_normal_poster_still_downloads(tmp_path: Path) -> None:
    respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=b"jpeg-bytes"))
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is True

    assert cache.is_cached(POSTER_URL)
    assert (tmp_path / POSTER_SUBDIR / cache.local_name(POSTER_URL)).read_bytes() == b"jpeg-bytes"


@respx.mock
async def test_a_failing_url_is_attempted_once_not_once_per_page_view(tmp_path: Path) -> None:
    """cache_posters awaits these before rendering, so a dead image host used to add the
    full download timeout to every poster-bearing page view."""
    route = respx.get(POSTER_URL).mock(return_value=httpx.Response(503))
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        for _ in range(5):
            assert await cache.ensure(client, POSTER_URL) is False

    assert route.call_count == 1


@respx.mock
async def test_an_oversized_url_is_also_only_attempted_once(tmp_path: Path) -> None:
    # Too big is as permanent as unreachable, and far more expensive to re-discover.
    served = {"megabytes": 0}
    route = respx.get(POSTER_URL).mock(
        side_effect=lambda request: httpx.Response(200, content=_endless_megabytes(served))
    )
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        for _ in range(3):
            assert await cache.ensure(client, POSTER_URL) is False

    assert route.call_count == 1


@respx.mock
async def test_a_failure_is_retried_once_it_is_old_enough(tmp_path: Path) -> None:
    """A blip must not cost the poster until the container restarts — the reason this
    is an expiring record rather than a permanent blacklist."""
    route = respx.get(POSTER_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=b"jpeg-bytes")]
    )
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is False
        # Age the recorded failure past the retry window.
        cache._failed_at[POSTER_URL] -= FAILED_RETRY_AFTER_SECONDS + 1
        assert await cache.ensure(client, POSTER_URL) is True

    assert route.call_count == 2
    assert cache.is_cached(POSTER_URL)


@respx.mock
async def test_an_already_cached_poster_is_never_re_requested(tmp_path: Path) -> None:
    route = respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=b"jpeg-bytes"))
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        await cache.ensure(client, POSTER_URL)
        await cache.ensure(client, POSTER_URL)

    assert route.call_count == 1


# --- TMDB-shaped URLs (TV step 10) ---
#
# The cache was written for Radarr's own metadata host. Television feeds it URLs that
# come straight off image.tmdb.org instead, and this section proves the three guarantees
# the TV pages are about to depend on hold for that shape too — before they depend on
# them, which is the whole reason this is its own step.

TMDB_POSTER = f"https://image.tmdb.org/t/p/{SERIES_POSTER_WIDTH}/abc123.jpg"


@respx.mock
async def test_a_tmdb_poster_is_cached_under_its_sha1(tmp_path: Path) -> None:
    """The name is a digest of the URL, so it is same-origin, opaque, and reveals
    nothing about where the bytes came from — which is what lets the CSP stay
    `img-src 'self'` with no third-party host allow-listed."""
    respx.get(TMDB_POSTER).mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg")
    )
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, TMDB_POSTER) is True

    expected = hashlib.sha1(TMDB_POSTER.encode()).hexdigest() + POSTER_SUFFIX  # noqa: S324
    assert cache.local_name(TMDB_POSTER) == expected
    assert (tmp_path / POSTER_SUBDIR / expected).read_bytes().startswith(b"\xff\xd8\xff")
    # And it serves only through the guard, by that name.
    assert cache.serve_path(expected) is not None


@respx.mock
async def test_the_size_cap_holds_against_a_tmdb_url(tmp_path: Path) -> None:
    """TMDB serves `original` at 1-3 MB and will serve it to anyone who asks for the
    wrong path. The cap is measured as the body ARRIVES, so an oversized image costs
    neither the disk nor the memory in a 256 MB container."""
    respx.get(TMDB_POSTER).mock(
        return_value=httpx.Response(200, content=b"x" * (MAX_POSTER_BYTES + 1))
    )
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, TMDB_POSTER) is False
    assert not cache.is_cached(TMDB_POSTER)


@respx.mock
async def test_a_failed_tmdb_poster_is_not_retried_on_every_render(tmp_path: Path) -> None:
    """A Discover shelf is six posters wide and re-renders often. Without the cooldown,
    one dead URL costs the download timeout on every single view of that page."""
    route = respx.get(TMDB_POSTER).mock(return_value=httpx.Response(404))
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, TMDB_POSTER) is False
        assert await cache.ensure(client, TMDB_POSTER) is False
        assert await cache.ensure(client, TMDB_POSTER) is False

    assert route.call_count == 1, "the dead URL was asked again inside its cooldown"
    assert FAILED_RETRY_AFTER_SECONDS > 0


@respx.mock
async def test_a_tmdb_url_survives_a_round_trip_through_sized(tmp_path: Path) -> None:
    """Every call site routes through `sized`, and the cache keys on the URL — so if
    `sized` rewrote a TMDB URL the fetch and the keep-set would disagree and maintenance
    would delete what the page had just downloaded."""
    composed = image_url("/abc123.jpg", SERIES_POSTER_WIDTH)
    assert composed == TMDB_POSTER
    assert sized(composed, SERIES_POSTER_WIDTH) == composed

    respx.get(TMDB_POSTER).mock(return_value=httpx.Response(200, content=b"jpeg"))
    cache = PosterCache(tmp_path)
    async with httpx.AsyncClient() as client:
        await cache.ensure(client, sized(composed, SERIES_POSTER_WIDTH))
    assert cache.is_cached(composed)


def test_series_and_film_widths_are_separate_and_series_is_smaller() -> None:
    """Series render smaller on BOTH their surfaces — a Discover strip is six across
    (~178px) and the show detail's poster is 160, against the movie grid's 208. One
    width per medium, so one cache entry per image."""
    assert SERIES_POSTER_WIDTH != POSTER_WIDTH
    assert int(SERIES_POSTER_WIDTH.lstrip("w")) < int(POSTER_WIDTH.lstrip("w"))


def test_the_two_widths_are_different_cache_entries(tmp_path: Path) -> None:
    """Which is exactly why a grid width and a detail width for one image would be two
    downloads, two files, and two things for `prune` to know about."""
    cache = PosterCache(tmp_path)
    film = sized(TMDB_POSTER, POSTER_WIDTH)
    assert cache.local_name(film) != cache.local_name(TMDB_POSTER)


# --- review step 7: the write does not run on the event loop ---


@respx.mock
async def test_the_poster_write_happens_off_the_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """A poster-heavy page caches one image per new title, and each write is a temp file,
    an fsync and a rename — plus however long the filestore lock is already held by a
    backup. On the loop thread that stops every other request for the duration.
    """
    respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=b"\xff\xd8\xff jpeg"))
    writing_threads: list[threading.Thread] = []
    real_write = posters.atomic_write_bytes

    def recording_write(target: Path, payload: bytes) -> None:
        writing_threads.append(threading.current_thread())
        real_write(target, payload)

    monkeypatch.setattr(posters, "atomic_write_bytes", recording_write)
    cache = PosterCache(tmp_path)

    async with httpx.AsyncClient() as client:
        assert await cache.ensure(client, POSTER_URL) is True

    assert writing_threads, "the poster was never written"
    assert writing_threads[0] is not threading.current_thread()
    assert cache.is_cached(POSTER_URL)  # and it really landed on disk
