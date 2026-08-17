"""Per-movie ignore list (never auto-add, on any week).

A movie the admin ignores is skipped by the pipeline forever — even when it charts
again in later weeks — until they un-ignore it. Matched by TMDB id when known
(stable across title variations), falling back to the normalized title for chart
entries Radarr couldn't identify.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.core import filestore
from app.core.audit import AuditLog

IGNORE_SCHEMA_VERSION = 1
IGNORE_FILENAME = "ignored.yml"
IGNORED_KEY = "ignored"
IGNORE_ADDED = "movie_ignored"
IGNORE_REMOVED = "movie_unignored"


class IgnoredMovie(BaseModel):
    tmdb_id: int | None = None
    title: str
    normalized_title: str

    def matches(self, tmdb_id: int | None, normalized_title: str) -> bool:
        """Whether a charted title is this entry — the same identity rule, one entry at a
        time, that `IgnoreSnapshot.is_ignored` applies across the whole list.

        Two films that both carry TMDB ids are the same film only if the ids agree; the
        title decides only when one side has no id to compare. Anything looser would tie
        a 1970 original to its 2026 remake, which normalize to the same title.
        """
        if self.tmdb_id is not None and tmdb_id is not None:
            return self.tmdb_id == tmdb_id
        return self.normalized_title == normalized_title


@dataclass(frozen=True)
class IgnoreSnapshot:
    """The ignore list resolved once, for answering many titles without re-reading it.

    Taken per render and per pipeline run, which also makes the answer internally
    consistent: every title on a page is judged against the same list, rather than
    against whatever the file happened to hold at the moment that row was built.
    """

    tmdb_ids: frozenset[int]
    titles: frozenset[str]
    # Titles of entries stored WITHOUT a TMDB id. Only these may match a chart entry that
    # Radarr did identify — otherwise ignoring the 1970 original would also hide a 2026
    # remake that happens to normalize to the same title.
    unidentified_titles: frozenset[str]

    def is_ignored(self, tmdb_id: int | None, normalized_title: str) -> bool:
        """Ignored when the TMDB ids match, or when either side has no id to compare.

        The title is a fallback for films that could not be identified, not a second way
        to match two films that were. Two entries that both carry ids and disagree are
        different films, however alike their titles read.
        """
        if tmdb_id is None:
            # Radarr could not identify this chart entry, so the title is all there is.
            return normalized_title in self.titles
        return tmdb_id in self.tmdb_ids or normalized_title in self.unidentified_titles


class IgnoreStore:
    def __init__(self, config_dir: Path, *, audit: AuditLog) -> None:
        self._path = config_dir / IGNORE_FILENAME
        self._audit = audit

    def _load(self) -> list[IgnoredMovie]:
        if not self._path.exists():
            return []
        document = filestore.read_yaml(self._path, expected_version=IGNORE_SCHEMA_VERSION)
        return [IgnoredMovie.model_validate(item) for item in document.get(IGNORED_KEY, [])]

    def _save(self, movies: list[IgnoredMovie]) -> None:
        filestore.write_yaml(
            self._path,
            {IGNORED_KEY: [movie.model_dump() for movie in movies]},
            schema_version=IGNORE_SCHEMA_VERSION,
        )

    def list_ignored(self) -> list[IgnoredMovie]:
        return self._load()

    def snapshot(self) -> IgnoreSnapshot:
        """Read the list once; ask it about as many titles as you like."""
        movies = self._load()
        return IgnoreSnapshot(
            tmdb_ids=frozenset(
                movie.tmdb_id for movie in movies if movie.tmdb_id is not None
            ),
            titles=frozenset(movie.normalized_title for movie in movies),
            unidentified_titles=frozenset(
                movie.normalized_title for movie in movies if movie.tmdb_id is None
            ),
        )

    def is_ignored(self, tmdb_id: int | None, normalized_title: str) -> bool:
        """One title, one file read. Callers judging many titles want `snapshot()`.

        Delegates so the matching rule lives in exactly one place — a second copy here
        would have to be changed in lockstep with IgnoreSnapshot forever.
        """
        return self.snapshot().is_ignored(tmdb_id, normalized_title)

    def add(self, *, tmdb_id: int | None, title: str, normalized_title: str) -> None:
        if self.is_ignored(tmdb_id, normalized_title):
            return
        movies = self._load()
        movies.append(
            IgnoredMovie(tmdb_id=tmdb_id, title=title, normalized_title=normalized_title)
        )
        self._save(movies)
        self._audit.record(IGNORE_ADDED, title=title, tmdb_id=tmdb_id)

    def remove(self, *, tmdb_id: int | None, normalized_title: str) -> None:
        def keep(movie: IgnoredMovie) -> bool:
            if tmdb_id is not None and movie.tmdb_id == tmdb_id:
                return False
            return movie.normalized_title != normalized_title

        movies = self._load()
        remaining = [movie for movie in movies if keep(movie)]
        if len(remaining) != len(movies):
            self._save(remaining)
            self._audit.record(IGNORE_REMOVED, title=normalized_title, tmdb_id=tmdb_id)
