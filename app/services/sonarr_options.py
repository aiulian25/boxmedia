"""Sonarr's own quality profiles and root folders, cached for the UI.

Deliberately thin. A quality profile and a root folder are the same concept on both
servers, and `radarr_options.fetch_options` already asks a *client* for them rather
than knowing which one it is — so the model and the fetch are imported, not restated.
Copying a hundred lines to change one filename would mean two places to fix the next
cache bug.

What Sonarr does get is its own FILE. The ids inside are per database — a Sonarr
profile id means nothing to Radarr — and keeping them apart means a glance at
`sonarr_options.yml` answers "what does my Sonarr offer" without reading past a
Radarr's entries. Both files are inside `/data/config`, so both ride the existing
backup with no change to what a backup contains.
"""

from __future__ import annotations

from pathlib import Path

from app.services.radarr_options import RadarrOptionsCache

SONARR_OPTIONS_FILENAME = "sonarr_options.yml"


class SonarrOptionsCache(RadarrOptionsCache):
    """Last-known profiles and root folders, per Sonarr connection."""

    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir, filename=SONARR_OPTIONS_FILENAME)
