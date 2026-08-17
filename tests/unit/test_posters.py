"""Poster cache hardening (Steps 5 + 18): size cap, atomic write, serve_path guard."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from app.services.posters import (
    FAILED_RETRY_AFTER_SECONDS,
    MAX_POSTER_BYTES,
    POSTER_SUBDIR,
    PosterCache,
)

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
