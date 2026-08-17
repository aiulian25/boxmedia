"""A stdlib-only stub serving a Box Office Mojo chart + a Radarr v3 API.

Used by the end-to-end smoke test (Step 24) so the containerized app can run its
whole pipeline against a controlled endpoint instead of the live internet.

Run: python tests/mock_radarr.py [port]
Serves:
  GET  /weekly                     -> the BOM chart fixture
  GET  /api/v3/system/status       -> {"version": "..."}
  GET  /api/v3/movie               -> library snapshot (one owned movie)
  GET  /api/v3/movie/lookup?term=  -> a single lookup match derived from the term
  POST /api/v3/movie               -> 201 (records the add)
  GET  /api/v3/qualityprofile      -> [{id,name}]
  GET  /api/v3/rootfolder          -> [{path}]
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bom_weekly.html"

# One movie already "in library" so the E2E can assert an IN_LIBRARY badge.
LIBRARY = [{"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "hasFile": True}]
ADDED: list[dict] = []
_NEXT_TMDB = [700000]

# A pool of real titles (so Radarr lookups still resolve) + one made-up one
# ("Blade Runner 2099") to exercise the no-match path. Different weeks show a
# different rotation of this pool, at different grosses.
_MOVIE_POOL = [
    ("Dune: Part Two", 81_500_000), ("Oppenheimer", 22_400_000),
    ("Gladiator II", 65_000_000), ("Furiosa: A Mad Max Saga", 50_100_000),
    ("Alien: Romulus", 41_200_000), ("Kraven the Hunter", 30_100_000),
    ("Mickey 17", 28_500_000), ("Civil War", 25_700_000),
    ("The Iron Claw", 22_100_000), ("Nosferatu", 18_900_000),
    ("Deadpool & Wolverine", 120_500_000), ("Poor Things", 15_800_000),
    ("Killers of the Flower Moon", 23_000_000), ("Godzilla Minus One", 11_000_000),
    ("The Boy and the Heron", 12_800_000), ("Anatomy of a Fall", 8_400_000),
    ("Past Lives", 7_200_000), ("Megalopolis", 14_200_000),
    ("Blade Runner 2099", 45_200_000),
]
_TOP_N = 10


def _week_chart_html(week_id: str) -> bytes:
    """Generate a top-10 chart whose contents vary by week id (mock only)."""
    seed = sum(ord(character) for character in week_id) if week_id else 0
    pool_size = len(_MOVIE_POOL)
    offset = seed % pool_size
    rotated = _MOVIE_POOL[offset:] + _MOVIE_POOL[:offset]
    rows = []
    for rank, (title, base_gross) in enumerate(rotated[:_TOP_N], start=1):
        gross = base_gross + (seed % 9) * 1_000_000
        weeks = (rank + seed) % 6 + 1
        rows.append(
            f'<tr><td class="mojo-field-type-rank">{rank}</td><td>-</td>'
            f'<td class="mojo-field-type-release_studios"><a>{title}</a></td>'
            f'<td class="mojo-field-type-money">${gross:,}</td><td>-</td><td>3,000</td>'
            f'<td class="mojo-field-type-money">$10,000</td>'
            f'<td class="mojo-field-type-money">${gross:,}</td>'
            f'<td class="mojo-field-type-positive_integer">{weeks}</td><td>Studio</td></tr>'
        )
    header = (
        '<tr><th class="mojo-field-type-rank">Rank</th><th>LW</th>'
        '<th class="mojo-field-type-release_studios">Release</th>'
        '<th class="mojo-field-type-money">Gross</th><th>%± LW</th><th>Theaters</th>'
        '<th class="mojo-field-type-money">Average</th>'
        '<th class="mojo-field-type-money">Total Gross</th>'
        '<th class="mojo-field-type-positive_integer">Weeks</th><th>Distributor</th></tr>'
    )
    html = (
        f"<html><body><h1>Weekly {week_id or 'current'}</h1>"
        f'<table class="mojo-body-table">{header}{"".join(rows)}</table></body></html>'
    )
    return html.encode("utf-8")


def _week_id_from_path(path: str) -> str:
    # /weekly -> "" (current); /weekly/2026W33/ -> "2026W33"
    parts = [segment for segment in path.split("/") if segment and segment != "weekly"]
    return parts[0] if parts else ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/weekly"):
            # Week-aware: each week gets a different rotation of the movie pool.
            self._send(
                200, _week_chart_html(_week_id_from_path(path)), "text/html; charset=utf-8"
            )
        elif path == "/api/v3/system/status":
            self._json({"version": "5.2.0-mock"})
        elif path == "/api/v3/movie":
            # Reflect manual adds so the review flow is observable end-to-end.
            added = [
                {
                    "tmdbId": item.get("tmdbId"), "id": index, "title": item.get("title"),
                    "year": item.get("year"), "hasFile": False,
                }
                for index, item in enumerate(ADDED, start=1)
            ]
            self._json(LIBRARY + added)
        elif path == "/api/v3/movie/lookup":
            term = parse_qs(parsed.query).get("term", ["Unknown"])[0]
            # Return a distinct tmdbId per term so non-library titles look "missing".
            tmdb = 693134 if term == "Dune: Part Two" else (900000 + abs(hash(term)) % 1000)
            self._json(
                [
                    {
                        "tmdbId": tmdb, "title": term, "year": 2025,
                        "overview": f"{term} overview.", "genres": ["Action"],
                        "imdbId": f"tt{tmdb}", "ratings": {"tmdb": {"value": 7.5}},
                        "images": [{"coverType": "poster", "remoteUrl": "http://example/p.jpg"}],
                    }
                ]
            )
        elif path == "/api/v3/qualityprofile":
            self._json([{"id": 4, "name": "HD-1080p"}])
        elif path == "/api/v3/rootfolder":
            self._json([{"path": "/movies"}])
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/v3/movie":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            ADDED.append(payload)
            _NEXT_TMDB[0] += 1
            self._json({**payload, "id": _NEXT_TMDB[0], "hasFile": False}, status=201)
        else:
            self._json({"error": "not found"}, status=404)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 59000
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 — test stub
    print(f"mock Radarr + BOM on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
