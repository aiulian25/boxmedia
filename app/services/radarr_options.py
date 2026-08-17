"""Radarr's own quality profiles and root folders, cached for the UI.

So the Settings and dashboard dropdowns show exactly what the connected Radarr
reports (no guessing profile IDs). The last successful fetch is cached to disk so
the dropdowns still work when Radarr is momentarily unreachable; if nothing has
ever been fetched, the UI falls back to plain text entry (never a dead-end).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.core import filestore

RADARR_OPTIONS_FILENAME = "radarr_options.yml"
# v2 keys the cache by connection id. Radarr assigns quality-profile ids per database, so
# one shared list would offer another instance's ids — a 4K profile id used against the
# 1080p box adds at whatever that id happens to mean there.
RADARR_OPTIONS_SCHEMA_VERSION = 2
BY_APP_KEY = "by_app"


class QualityProfile(BaseModel):
    id: int
    name: str


class RadarrOptions(BaseModel):
    profiles: list[QualityProfile] = Field(default_factory=list)
    root_folders: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.profiles and not self.root_folders

    def profile_name(self, profile_id: int | None) -> str | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile.name
        return None


async def fetch_options(client: object) -> RadarrOptions:
    """Pull the live profiles + root folders from a Radarr client."""
    profiles = await client.quality_profiles()  # list[(id, name)]
    root_folders = await client.root_folders()  # list[str]
    return RadarrOptions(
        profiles=[QualityProfile(id=identifier, name=name) for identifier, name in profiles],
        root_folders=list(root_folders),
    )


class RadarrOptionsCache:
    """Last-known profiles and root folders, per connection."""

    def __init__(self, config_dir: Path) -> None:
        self._path = config_dir / RADARR_OPTIONS_FILENAME

    def _load_all(self) -> dict[str, RadarrOptions]:
        if not self._path.exists():
            return {}
        document = filestore.read_yaml(self._path, expected_version=RADARR_OPTIONS_SCHEMA_VERSION)
        document.pop(filestore.SCHEMA_VERSION_KEY, None)
        by_app = document.get(BY_APP_KEY)
        if not isinstance(by_app, dict):
            # A v1 file (one un-keyed blob). This is a cache, not a record — drop it and
            # let the next successful fetch repopulate it per connection.
            return {}
        return {
            app_id: RadarrOptions.model_validate(entry) for app_id, entry in by_app.items()
        }

    def load(self, app_id: str | None) -> RadarrOptions:
        """One connection's options, or empty when nothing has been cached for it."""
        if app_id is None:
            return RadarrOptions()
        return self._load_all().get(app_id, RadarrOptions())

    def load_all(self) -> dict[str, RadarrOptions]:
        """Every cached connection, in one read — `load` per connection re-reads the file."""
        return self._load_all()

    def save(self, app_id: str, options: RadarrOptions) -> None:
        stored = self._load_all()
        stored[app_id] = options
        self._write(stored)

    def forget(self, app_id: str) -> None:
        """Drop a removed connection's entry so the file cannot grow forever."""
        stored = self._load_all()
        if stored.pop(app_id, None) is not None:
            self._write(stored)

    def _write(self, stored: dict[str, RadarrOptions]) -> None:
        filestore.write_yaml(
            self._path,
            {BY_APP_KEY: {app_id: options.model_dump() for app_id, options in stored.items()}},
            schema_version=RADARR_OPTIONS_SCHEMA_VERSION,
        )
