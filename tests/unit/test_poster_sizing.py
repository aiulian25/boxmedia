"""TMDB image sizing — every poster path keys on the same URL form."""

from __future__ import annotations

import pytest

from app.services.posters import HEADSHOT_WIDTH, POSTER_WIDTH, sized

TMDB = "https://image.tmdb.org/t/p/{}/abc123.jpg"


@pytest.mark.parametrize("current", ["original", "w500", "w1280", "h632"])
def test_any_tmdb_size_is_rewritten(current: str) -> None:
    assert sized(TMDB.format(current), POSTER_WIDTH) == TMDB.format(POSTER_WIDTH)


def test_headshots_use_their_own_width() -> None:
    assert sized(TMDB.format("original"), HEADSHOT_WIDTH) == TMDB.format(HEADSHOT_WIDTH)


@pytest.mark.parametrize(
    "url",
    [
        "http://radarr.local/MediaCover/1/poster.jpg",  # Radarr-hosted, not TMDB
        "https://example.test/some/poster.jpg",
        "https://image.tmdb.org/t/p/",  # malformed — no size segment
        "",
    ],
)
def test_non_tmdb_urls_pass_through_untouched(url: str) -> None:
    assert sized(url, POSTER_WIDTH) == url


def test_none_stays_none() -> None:
    assert sized(None, POSTER_WIDTH) is None


def test_rewriting_is_idempotent() -> None:
    once = sized(TMDB.format("original"), POSTER_WIDTH)
    assert sized(once, POSTER_WIDTH) == once


def test_poster_width_is_not_wildly_oversized_for_the_grid() -> None:
    # The grid box is 208 CSS px. A large downscale ratio is what a GPU rasterizer
    # renders badly, so keep the served width within a few multiples of the box.
    grid_box_px = 208
    served = int(POSTER_WIDTH.lstrip("w"))
    assert served >= grid_box_px * 2  # still sharp on a 2x display
    assert served <= grid_box_px * 3  # but not a 9x downscale like `original` was
