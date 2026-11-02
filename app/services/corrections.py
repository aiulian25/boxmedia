"""Which film a chart title really is, once a human has said so.

Box Office Mojo prints a title its own way — "Miroirs No. 3" for a film Radarr knows as
"Mirrors No. 3" — and where the spellings diverge, no amount of matching will close the
gap. The fix-match flow exists for exactly that, and until now its answer lived in one row
of one week: the same film in the week before stayed wrong, and the next automatic check
overwrote the corrected row with a fresh guess.

A correction is a fact about a CHART TITLE, not about a row. Stored that way, it survives
every re-run, reaches every week the title charts in, and outranks anything the matcher
would have decided on its own — a human looked at both posters, which is the strongest
signal this app will ever have.

Keyed by the chart release when the row recorded one, because a title is not an identity:
two films called Nosferatu chart under one normalized title, years apart. Corrections made
before rows carried that, or on a row without it, stay keyed by the normalized chart title.
Corrections are made one at a time by an admin, so this file grows by hand and needs no
ceiling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.core import filestore

CORRECTIONS_SCHEMA_VERSION = 1
CORRECTIONS_FILENAME = "corrections.yml"
BY_TITLE_KEY = "by_title"
BY_RELEASE_KEY = "by_release"
_IMDB_ID_RE = re.compile(r"tt\d+")


class Correction(BaseModel):
    """The film an admin confirmed, as Radarr described it at the moment they confirmed.

    Everything the card needs is captured here rather than re-looked-up later: the run
    that applies this may be unattended, and a correction that stopped working because
    Radarr's search phrasing drifted would be a correction that did not correct.
    """

    tmdb_id: int
    title: str
    year: int | None = None
    imdb_url: str | None = None
    poster_url: str | None = None
    # Which chart release the admin was looking at, when the row recorded it. Set: this is
    # about that release. None: an older correction, or a row with no source record, and it
    # can only speak for the title as a whole.
    release_path: str | None = None
    imdb_id: str | None = None

    def film_imdb_id(self) -> str | None:
        """Its IMDb id, read off the link for a correction written before it was stored."""
        if self.imdb_id:
            return self.imdb_id
        match = _IMDB_ID_RE.search(self.imdb_url or "")
        return match.group(0) if match else None


@dataclass(frozen=True)
class CorrectionBook:
    """Every correction read once, for judging a whole chart against the same answers."""

    by_title: dict[str, Correction]
    by_release: dict[str, Correction]

    def for_row(
        self, normalized_title: str, release_path: str | None, source_imdb_id: str | None
    ) -> Correction | None:
        """The correction this chart row is really about, or None.

        Strongest first: this exact release, then the same film on another release (a
        re-release has its own path). A title-only correction comes last and is kept in
        force, since it is a decision somebody made and it cannot be split after the fact,
        unless the source says this row is a different film: a remake charting years later
        has the title, not the film.
        """
        scoped = self.by_release.get(release_path) if release_path else None
        if scoped is not None:
            return scoped
        if source_imdb_id:
            for correction in self.by_release.values():
                if correction.film_imdb_id() == source_imdb_id:
                    return correction
        by_title = self.by_title.get(normalized_title)
        if by_title is None:
            return None
        film = by_title.film_imdb_id()
        if source_imdb_id and film and film != source_imdb_id:
            return None
        return by_title


class CorrectionStore:
    """Confirmed film identities, by chart release and, for older ones, by chart title."""

    def __init__(self, config_dir: Path) -> None:
        self._path = config_dir / CORRECTIONS_FILENAME

    def book(self) -> CorrectionBook:
        if not self._path.exists():
            return CorrectionBook({}, {})
        document = filestore.read_yaml(
            self._path, expected_version=CORRECTIONS_SCHEMA_VERSION
        )
        return CorrectionBook(
            by_title=_section(document, BY_TITLE_KEY),
            by_release=_section(document, BY_RELEASE_KEY),
        )

    def get(self, normalized_title: str) -> Correction | None:
        return self.book().by_title.get(normalized_title)

    def all(self) -> dict[str, Correction]:
        """Every title-wide correction in one read."""
        return self.book().by_title

    def save(self, normalized_title: str, correction: Correction) -> None:
        """Record a confirmation, replacing any earlier one with the same scope.

        Replacing rather than refusing is what makes a wrong correction recoverable: the
        admin fixes the row again and the new answer simply takes over. Filed under its
        release when it has one, so a namesake on another release keeps its own answer.
        """
        book = self.book()
        if correction.release_path:
            book.by_release[correction.release_path] = correction
        else:
            book.by_title[normalized_title] = correction
        filestore.write_yaml(
            self._path,
            {
                BY_TITLE_KEY: {key: entry.model_dump() for key, entry in book.by_title.items()},
                BY_RELEASE_KEY: {
                    key: entry.model_dump() for key, entry in book.by_release.items()
                },
            },
            schema_version=CORRECTIONS_SCHEMA_VERSION,
        )


def _section(document: dict, key: str) -> dict[str, Correction]:
    stored = document.get(key)
    if not isinstance(stored, dict):
        return {}
    return {name: Correction.model_validate(entry) for name, entry in stored.items()}
