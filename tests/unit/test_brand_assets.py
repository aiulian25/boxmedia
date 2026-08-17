"""The served logo must be the artwork alone — no background plate, on any surface.

Guards the output of scripts/build-icons.py rather than the script itself: what ships is
the committed asset, so that is what gets asserted. Pillow is a declared dev dependency.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parent.parent.parent / "app" / "static"
ICO_SIZES = {(16, 16), (32, 32), (48, 48)}
# The master's tile: a near-neutral grey. Anything opaque in this range is plate.
PLATE_MIN, PLATE_MAX = 25, 70
NEUTRAL_SPREAD = 6
OPAQUE = 200
# Centring on a square canvas leaves at most this much slack on the narrow axis.
MAX_SIDE_MARGIN = 8


def _plate_pixels(image: Image.Image) -> int:
    return sum(
        1
        for red, green, blue, alpha in image.convert("RGBA").getdata()
        if alpha > OPAQUE
        and max(red, green, blue) - min(red, green, blue) <= NEUTRAL_SPREAD
        and PLATE_MIN <= red <= PLATE_MAX
    )


def test_logo_has_no_background_plate() -> None:
    assert _plate_pixels(Image.open(STATIC / "logo.png")) == 0


def test_logo_corners_are_transparent() -> None:
    logo = Image.open(STATIC / "logo.png").convert("RGBA")
    width, height = logo.size
    corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    assert [logo.getpixel(point)[3] for point in corners] == [0, 0, 0, 0]


def test_logo_artwork_fills_its_box() -> None:
    # Reframed, not inset: a halo would make the mark render small at 16px.
    logo = Image.open(STATIC / "logo.png").convert("RGBA")
    left, top, right, bottom = logo.getchannel("A").getbbox()
    width, height = logo.size
    assert (top, bottom) == (0, height)  # spans the full height
    assert right - left >= width - MAX_SIDE_MARGIN * 2


def test_favicon_ships_every_icon_size() -> None:
    icon = Image.open(STATIC / "favicon.ico")
    assert set(icon.ico.sizes()) == ICO_SIZES
    assert _plate_pixels(icon.ico.getimage((32, 32))) == 0
