"""Which IMDb title a Mojo release page belongs to, remembered so it is asked once.

A release page is fetched only when Radarr could not recognise a chart title, and the
answer never changes: `/release/rl1234/` is one film forever. Caching it on disk is what
keeps the confirmation to one extra request per film for the life of the install, rather
than one per week it stays on the chart.

Successes only. A page that could not be read is not remembered as "no id" — that would
pin a moment's outage to a film permanently, and the alternative costs one request the
next time the film is guessed at, capped by the pipeline's per-run budget.

Unbounded by design: an entry is roughly eighty bytes and only a title Radarr could not
match earns one, so a busy install adds a few hundred a year. If that ever needs a
ceiling, the natural one is to drop entries for release paths no stored report mentions.
"""

from __future__ import annotations

from pathlib import Path

from app.core import filestore

RELEASE_IDS_FILENAME = "release-imdb-ids.json"
RELEASE_IDS_SCHEMA_VERSION = 1
IDS_KEY = "ids"


class ReleaseIdCache:
    """A release path to its IMDb id, on disk under the cache directory.

    A cache, not a record: a file that cannot be read is treated as empty rather than
    raising, because losing it costs a re-fetch and refusing to start costs the run.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / RELEASE_IDS_FILENAME

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            document = filestore.read_json(
                self._path, expected_version=RELEASE_IDS_SCHEMA_VERSION
            )
        except (ValueError, OSError):
            # Unreadable, not JSON, or stamped by a newer build (SchemaVersionError is a
            # ValueError). All three cost a re-fetch; none is worth failing a run over.
            return {}
        ids = document.get(IDS_KEY)
        if not isinstance(ids, dict):
            return {}
        return {
            path: value
            for path, value in ids.items()
            if isinstance(path, str) and isinstance(value, str)
        }

    def get(self, release_path: str | None) -> str | None:
        """The remembered id for a path, or None when it has never been resolved."""
        if release_path is None:
            return None
        return self._load().get(release_path)

    def put(self, release_path: str, imdb_id: str) -> None:
        stored = self._load()
        if stored.get(release_path) == imdb_id:
            return  # nothing to write, so a settled install stops touching the disk
        stored[release_path] = imdb_id
        filestore.write_json(
            self._path, {IDS_KEY: stored}, schema_version=RELEASE_IDS_SCHEMA_VERSION
        )
