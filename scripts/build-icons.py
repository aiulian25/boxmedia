#!/usr/bin/env python3
"""Derive the served icon set from the master logo.

The master is a flat RGB export: the artwork sits on a dark rounded tile, which in turn
sits on a white page. Neither belongs in the served icon — the tile reads as a lighter
square wherever BoxMedia's chrome is a different shade, which is every surface it appears
on. Both are dropped here so only the box and film reel are served, on transparency.

The key is saturation, not colour matching: the artwork is saturated orange and yellow
while the tile and the page are pure greys, so one pass removes both. The ramp between
the thresholds keeps antialiased edges from fringing.

Dev-only — Pillow is not a runtime dependency. Run it after changing the master:

    .venv/bin/python scripts/build-icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "brand" / "boxmedia-logo.png"
STATIC = ROOT / "app" / "static"
ICO_SIZES = [(16, 16), (32, 32), (48, 48)]
LOGO_SIZE = 256
# Saturation below the floor is background (grey tile, white page); above the ceiling is
# artwork. In between the alpha ramps, which is what keeps the edges clean.
SATURATION_FLOOR = 40
SATURATION_CEILING = 90
OPAQUE = 255


def _key_out_background(image: Image.Image) -> Image.Image:
    """Alpha from colour saturation — drops the tile and the page in one pass."""
    saturation = image.convert("HSV").getchannel("S")
    span = SATURATION_CEILING - SATURATION_FLOOR

    def opacity(value: int) -> int:
        if value <= SATURATION_FLOOR:
            return 0
        if value >= SATURATION_CEILING:
            return OPAQUE
        return (value - SATURATION_FLOOR) * OPAQUE // span

    keyed = image.convert("RGBA")
    keyed.putalpha(saturation.point(opacity))
    return keyed


def _square(image: Image.Image, size: int) -> Image.Image:
    """Trim to the artwork and centre it on a square canvas, so the mark fills the space
    it is given at 16px without being stretched out of proportion."""
    box = image.getchannel("A").getbbox()
    artwork = image.crop(box) if box else image
    side = max(artwork.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(artwork, ((side - artwork.width) // 2, (side - artwork.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    artwork = _key_out_background(Image.open(MASTER).convert("RGB"))

    STATIC.mkdir(parents=True, exist_ok=True)
    _square(artwork, LOGO_SIZE).save(STATIC / "logo.png", optimize=True)
    _square(artwork, max(width for width, _ in ICO_SIZES)).save(
        STATIC / "favicon.ico", sizes=ICO_SIZES
    )

    for name in ("logo.png", "favicon.ico"):
        print(f"wrote {STATIC / name} ({(STATIC / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
