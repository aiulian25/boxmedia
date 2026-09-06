"""Regenerate the README screenshots from fictional sample data.

Committed rather than done by hand, for three reasons. The README promises every
screenshot uses sample data — a script is the only version of that promise anyone can
CHECK. Artwork is generated here as plain gradients with the title typeset on them, so
no copyrighted poster is ever reproduced. And a UI change stops meaning "someone has to
remember to retake five images in the same window size with the same seed".

Nothing real goes in: the titles are invented, the person is invented, and every address
is from **192.0.2.0/24** — TEST-NET-1, the range RFC 5737 reserves for documentation
precisely so examples cannot point at somebody's actual machine.

Dev tooling, not shipped code: Pillow and Chrome are used here and are not application
dependencies (ruling 3 is untouched — `pyproject.toml` is not changed).

    .venv/bin/python scripts/screenshots.py

Writes docs/screenshots/*.png at 1440x900, the size the existing set was taken at.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import respx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.calendar import (  # noqa: E402
    KIND_MOVIE,
    KIND_SERIES,
    STATE_DOWNLOADED,
    STATE_DOWNLOADING,
    STATE_MISSING,
    STATE_MONITORED,
    STATE_TODAY,
    CalendarEntry,
)
from app.services.discovery import (  # noqa: E402
    ANTICIPATED_KEY,
    TRENDING_KEY,
    DiscoverShow,
)
from app.services.matcher import normalize_title  # noqa: E402
from app.services.mediaserver import (  # noqa: E402
    MediaServerFetch,
    MediaServerSeries,
)
from app.services.reports import (  # noqa: E402
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from app.services.sonarr import SonarrSeries  # noqa: E402
from tests.conftest import build_harness  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
WIDTH, HEIGHT = 1440, 900

# TEST-NET-1 (RFC 5737). Reserved for documentation, routable nowhere.
RADARR_URL = "http://192.0.2.10:7878"
SONARR_URL = "http://192.0.2.11:8989"
PLEX_URL = "http://192.0.2.12:32400"
RADARR_NAME = "Attic Radarr"
SONARR_NAME = "Attic Sonarr"
# Not a real key. Sixteen bytes of nothing, and Settings masks a saved one anyway.
SAMPLE_KEY = "0" * 32
DISPLAY_NAME = "Robin Vale"

POSTER_BASE = "https://image.tmdb.org/t/p/original"

# Invented titles. Nothing here names a real film or series, and nothing reproduces
# anyone's artwork — the posters below are gradients this script draws.
FILMS = [
    ("Neon Rain", (37, 99, 168), MovieStatus.IN_LIBRARY, 81_500_000, 4),
    ("Skin Crawl", (167, 45, 55), MovieStatus.WANTED, 45_200_000, 5),
    ("The Long Static", (98, 88, 156), MovieStatus.IN_LIBRARY, 24_100_000, 4),
    ("Midnight Freight", (150, 99, 40), MovieStatus.WANTED, 14_900_000, 3),
    ("Copper Sky", (176, 128, 66), MovieStatus.IN_LIBRARY, 11_200_000, 4),
]
SERIES = [
    ("The Hollow Coast", (30, 92, 96), 2023, 34, 34),
    ("Sodium Lights", (120, 74, 40), 2024, 21, 13),
    ("Chorus of Static", (64, 70, 130), 2022, 48, 45),
    ("Low Orbit", (44, 92, 62), 2026, 16, 16),
    ("The Paper Republic", (128, 56, 84), 2025, 10, 4),
]
TRENDING = [
    ("The Hollow Coast", 2023, 1842, (30, 92, 96)),
    ("Verdigris", 2026, 1310, (52, 96, 78)),
    ("Sodium Lights", 2024, 1104, (120, 74, 40)),
    ("The Paper Republic", 2025, 903, (128, 56, 84)),
    ("Chorus of Static", 2022, 774, (64, 70, 130)),
]
ANTICIPATED = [
    ("Ash Cartography", 2027, 5127, (58, 66, 92)),
    ("The Salt Line", 2027, 4410, (96, 60, 58)),
    ("Gravel and Gold", 2026, 3980, (140, 112, 52)),
    ("Bright Harbour", 2027, 2874, (40, 84, 124)),
    ("Winter Assay", 2027, 2310, (72, 84, 100)),
]


def _poster(title: str, tint: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    """A poster-shaped gradient with the title set on it.

    Drawn rather than fetched, which is the whole point: a screenshot that showed real
    cover art would be reproducing somebody's copyrighted work to advertise this one.
    """
    width, height = size
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for row in range(height):
        fade = row / height
        draw.line(
            [(0, row), (width, row)],
            fill=tuple(int(channel * (1 - 0.55 * fade)) for channel in tint),
        )
    font = _font(int(width / 11))
    lines = _wrap(title.upper(), font, width - int(width * 0.16), draw)
    line_height = int(width / 9)
    top = height - int(height * 0.22) - line_height * (len(lines) - 1)
    for index, line in enumerate(lines):
        draw.text(
            (int(width * 0.08), top + index * line_height),
            line, font=font, fill=(238, 238, 238),
        )
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def _wrap(text: str, font: ImageFont.FreeTypeFont, limit: int, draw: ImageDraw.ImageDraw):
    lines, current = [], ""
    for word in text.split():
        attempt = f"{current} {word}".strip()
        if current and draw.textlength(attempt, font=font) > limit:
            lines.append(current)
            current = word
        else:
            current = attempt
    return [*lines, current] if current else lines


def _poster_url(title: str) -> str:
    return f"{POSTER_BASE}/{normalize_title(title).replace(' ', '-')}.jpg"


def _serve_posters() -> None:
    """Answer every artwork request with a gradient drawn for that title."""
    tints: dict[str, tuple[int, int, int]] = {}
    for title, tint, *_ in FILMS:
        tints[title] = tint
    for title, tint, *_ in SERIES:
        tints[title] = tint
    for title, _, _, tint in TRENDING + ANTICIPATED:
        tints[title] = tint

    def answer(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.rsplit("/", 1)[-1].removesuffix(".jpg")
        wide = "/w500/" in str(request.url)
        title = next(
            (name for name in tints if normalize_title(name).replace(" ", "-") == slug),
            "Untitled",
        )
        size = (500, 750) if wide else (342, 513)
        return httpx.Response(200, content=_poster(title, tints.get(title, (60, 60, 70)), size))

    respx.get(url__startswith="https://image.tmdb.org").mock(side_effect=answer)


def _show(title: str, year: int, *, watchers: int | None = None,
          listed: int | None = None) -> DiscoverShow:
    """One Trakt row. A show BoxMedia already holds carries the tvdb id the snapshot
    knows, so the card resolves to "In Sonarr"; the rest carry ids nothing holds, which
    is what leaves them Wanted — the mix the shelf exists to show."""
    held = [name for name, *_ in SERIES]
    tvdb = 100 + held.index(title) + 1 if title in held else 400 + len(title)
    return DiscoverShow(
        tmdb_id=2000 + tvdb, tvdb_id=tvdb, imdb_id=None, trakt_id=tvdb,
        title=title, year=year, overview="", poster_url=_poster_url(title),
        watchers=watchers, list_count=listed,
    )


def _sonarr_series(index: int, title: str, year: int, total: int, have: int) -> SonarrSeries:
    return SonarrSeries(
        sonarr_id=index, tvdb_id=100 + index, title=title, year=year,
        monitored=True, ended=False, episode_count=total, episode_file_count=have,
        poster_url=_poster_url(title), path=f"/tv/{normalize_title(title)}",
        imdb_id=None, tmdb_id=1400 + index,
        title_slug=normalize_title(title).replace(" ", "-"),
    )


def _seed(harness) -> None:
    apps = harness.client.app.state.apps
    apps.add(name=RADARR_NAME, url=RADARR_URL, api_key=SAMPLE_KEY)
    apps.add(name=SONARR_NAME, url=SONARR_URL, api_key=SAMPLE_KEY, kind="sonarr")
    radarr_id = apps.list_apps("radarr")[0].id
    sonarr_id = apps.list_apps("sonarr")[0].id
    apps.set_defaults(radarr_id, quality_profile_id=4, root_folder="/movies")
    apps.set_defaults(
        sonarr_id, quality_profile_id=4, root_folder="/tv",
        series_type="standard", season_folders=True, search_on_add=True,
    )
    harness.client.app.state.discovery.save("tmdb", "a" * 32)
    harness.client.app.state.discovery.save("trakt", "b" * 64)
    # The form takes all three or none — an invented person, an invented address.
    harness.client.post(
        "/account/profile",
        data={"username": "admin", "display_name": DISPLAY_NAME,
              "email": "robin@example.invalid"},
        follow_redirects=False,
    )

    harness.client.app.state.reports.save(Report(
        id="report-20260824-060000-samp", week="2026W34",
        run_at="2026-08-24T06:00:00+00:00", trigger=RunTrigger.SCHEDULED,
        status=RunStatus.OK, totals=ReportTotals(movies=len(FILMS), matched=3),
        movies=[
            MovieResult(
                rank=rank, title=title, normalized_title=normalize_title(title),
                gross_amount=gross, gross_display=f"${gross / 1_000_000:.1f}M",
                weeks_in_release=weeks, status=status, action=MovieAction.NONE,
                tmdb_id=5000 + rank, year=2026, poster_url=_poster_url(title),
                total_gross=gross * 2 + 15_000_000,
                imdb_url=f"https://www.imdb.com/title/tt{5000 + rank}/",
                wiki_url=f"https://en.wikipedia.org/wiki/Special:Search?search={title}",
            )
            for rank, (title, _, status, gross, weeks) in enumerate(FILMS, start=1)
        ],
    ))

    harness.client.app.state.series_cache.save(sonarr_id, tuple(
        _sonarr_series(index, title, year, total, have)
        for index, (title, _, year, total, have) in enumerate(SERIES, start=1)
    ))
    # One trending show the media server already holds but Sonarr does not — the state
    # the show library exists to surface.
    harness.client.post(
        "/settings/media-server",
        data={"url": PLEX_URL, "token": "t" * 20, "kind": "plex"},
        follow_redirects=False,
    )
    harness.client.app.state.media_server_cache.save(MediaServerFetch(
        movies=(), truncated=False,
        series=(MediaServerSeries(title="Verdigris", year=2026, tvdb_id=402),),
    ))

    harness.client.app.state.discover_cache.save({
        TRENDING_KEY: tuple(
            _show(title, year, watchers=watchers)
            for title, year, watchers, _ in TRENDING
        ),
        ANTICIPATED_KEY: tuple(
            _show(title, year, listed=listed)
            for title, year, listed, _ in ANTICIPATED
        ),
    })

    now = datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return monday + timedelta(days=day, hours=hour, minutes=minute)

    today = now.weekday()
    harness.client.app.state.calendar_cache.save([
        CalendarEntry(KIND_SERIES, "The Hollow Coast", "S03E07 · 21:00", at(0, 21),
                      STATE_DOWNLOADED, SONARR_NAME),
        CalendarEntry(KIND_SERIES, "Sodium Lights", "S02E09 · 22:00", at(0, 22),
                      STATE_DOWNLOADING, SONARR_NAME, progress=62.0),
        CalendarEntry(KIND_MOVIE, "Copper Sky", "Physical release", at(1, 9),
                      STATE_DOWNLOADED, RADARR_NAME),
        CalendarEntry(KIND_SERIES, "Chorus of Static", "S03E12 · 20:30", at(1, 20, 30),
                      STATE_MISSING, SONARR_NAME),
        CalendarEntry(KIND_MOVIE, "Midnight Freight", "Digital release", at(today, 6),
                      STATE_MISSING, RADARR_NAME),
        CalendarEntry(KIND_SERIES, "Low Orbit", "S02E08 · 23:00", at(today, 23),
                      STATE_TODAY, SONARR_NAME),
        CalendarEntry(KIND_SERIES, "The Paper Republic", "S01E01 · 21:00", at(4, 21),
                      STATE_MONITORED, SONARR_NAME),
        CalendarEntry(KIND_SERIES, "The Hollow Coast", "S03E08 · 21:00", at(4, 21, 45),
                      STATE_MONITORED, SONARR_NAME),
        CalendarEntry(KIND_MOVIE, "Skin Crawl", "In cinemas", at(6, 12),
                      STATE_MONITORED, RADARR_NAME),
    ])


def _connections() -> None:
    """What the two servers answer. Settings probes profiles and folders live, so a
    screenshot of it needs them — these are the sample values the cards then show."""
    for base in (RADARR_URL, SONARR_URL):
        respx.get(f"{base}/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "5.0.0"})
        )
        respx.get(f"{base}/api/v3/qualityprofile").mock(
            return_value=httpx.Response(200, json=[
                {"id": 4, "name": "HD-1080p"}, {"id": 6, "name": "Ultra-HD"},
            ])
        )
        respx.get(f"{base}/api/v3/rootfolder").mock(
            return_value=httpx.Response(200, json=[
                {"path": "/movies" if base == RADARR_URL else "/tv", "freeSpace": 4 * 10**12},
            ])
        )
    respx.get(f"{SONARR_URL}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith=f"{SONARR_URL}/api/v3/calendar").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(url__startswith=f"{RADARR_URL}/api/v3/calendar").mock(
        return_value=httpx.Response(200, json=[])
    )


def _radarr_library() -> None:
    respx.get(f"{RADARR_URL}/api/v3/movie").mock(return_value=httpx.Response(200, json=[
        {"id": 10 + rank, "tmdbId": 5000 + rank, "title": title, "year": 2026,
         "hasFile": status == MovieStatus.IN_LIBRARY,
         "titleSlug": normalize_title(title).replace(" ", "-"),
         "movieFile": ({"quality": {"quality": {"name": "Bluray-1080p"}}}
                       if status == MovieStatus.IN_LIBRARY else None)}
        for rank, (title, _, status, _, _) in enumerate(FILMS, start=1)
    ]))
    respx.get(url__regex=r"https?://[^/]+/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [
            {"movieId": 12, "size": 1000, "sizeleft": 380},
        ]})
    )


def _chrome() -> str:
    """A headless browser to photograph with, by absolute path."""
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("no Chrome or Chromium on PATH — cannot take screenshots")


def _capture(harness, path: str, name: str, workspace: Path,
             height: int = HEIGHT) -> None:
    """Render one page to a file and photograph it at the README's window size."""
    page = harness.client.get(path)
    if page.status_code != 200:
        raise RuntimeError(f"{path} answered {page.status_code}")
    markup = page.text
    markup = re.sub(r'href="[^"]*app\.css[^"]*"', 'href="app.css"', markup)
    markup = re.sub(r'(src|href)="[^"]*logo\.png[^"]*"', r'\1="logo.png"', markup)
    markup = markup.replace('src="/posters/', 'src="posters/')
    document = workspace / f"{name}.html"
    document.write_text(markup, encoding="utf-8")

    subprocess.run(  # noqa: S603 — resolved binary, rendering a file this script wrote
        [_chrome(), "--headless", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--window-size={WIDTH},{height}",
         f"--screenshot={OUT / f'{name}.png'}", document.as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    print(f"  {name}.png")


# Settings is one long page, so it is photographed whole and then cropped to the two
# cards television added. Offsets into that tall render — re-check them after a change
# to the Settings layout, which is what the blank-crop guard below is for.
SETTINGS_HEIGHT = 3600
SETTINGS_CROPS = {
    "settings-sonarr": (1180, 2080),
    "settings-discovery": (2360, 2900),
}


def _crop_settings() -> None:
    """Two cards out of one long page. The whole render is a means, not an output — the
    page top is the account form, which says nothing about television."""
    tall = OUT / "settings.png"
    whole = Image.open(tall).convert("RGB")
    bands = {
        name: whole.crop((0, top, WIDTH, bottom))
        for name, (top, bottom) in SETTINGS_CROPS.items()
    }
    for name, band in bands.items():
        # A layout shift would otherwise write a plausible-looking rectangle of
        # background. Few distinct colours means the crop found nothing.
        if len(band.getcolors(maxcolors=1 << 20) or []) < 50:
            raise RuntimeError(
                f"{name}.png is nearly blank — the Settings layout moved,"
                " re-check SETTINGS_CROPS"
            )
        band.save(OUT / f"{name}.png")
        print(f"  {name}.png")
    tall.unlink()


@respx.mock
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _serve_posters()
    _connections()
    _radarr_library()

    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch)
        harness = build_harness(workspace / "data")
        harness.activate()
        _seed(harness)

        shutil.copy(ROOT / "app/static/css/app.css", workspace / "app.css")
        shutil.copy(ROOT / "app/static/logo.png", workspace / "logo.png")

        print("Capturing:")
        for path, name, height in (
            ("/library", "library", HEIGHT),
            ("/discover", "discover", 1250),
            ("/calendar", "calendar", HEIGHT),
            ("/settings", "settings", SETTINGS_HEIGHT),
        ):
            # Rendered once first so every poster is in the cache before the photograph.
            harness.client.get(path)
            posters = workspace / "posters"
            shutil.rmtree(posters, ignore_errors=True)
            shutil.copytree(harness.settings.cache_dir / "posters", posters,
                            dirs_exist_ok=True)
            _capture(harness, path, name, workspace, height)
        _crop_settings()
    return 0


if __name__ == "__main__":
    sys.exit(main())
