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

Keyed by the normalized chart title, because that is what a future run will have in hand
before it knows anything else about the row. Corrections are made one at a time by an
admin, so this file grows by hand and needs no ceiling.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core import filestore

CORRECTIONS_SCHEMA_VERSION = 1
CORRECTIONS_FILENAME = "corrections.yml"
BY_TITLE_KEY = "by_title"


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


class CorrectionStore:
    """Confirmed chart-title-to-film mappings, by normalized chart title."""

    def __init__(self, config_dir: Path) -> None:
        self._path = config_dir / CORRECTIONS_FILENAME

    def _load(self) -> dict[str, Correction]:
        if not self._path.exists():
            return {}
        document = filestore.read_yaml(
            self._path, expected_version=CORRECTIONS_SCHEMA_VERSION
        )
        stored = document.get(BY_TITLE_KEY)
        if not isinstance(stored, dict):
            return {}
        return {
            title: Correction.model_validate(entry) for title, entry in stored.items()
        }

    def get(self, normalized_title: str) -> Correction | None:
        return self._load().get(normalized_title)

    def all(self) -> dict[str, Correction]:
        """Every correction in one read, for a run that judges a whole chart."""
        return self._load()

    def save(self, normalized_title: str, correction: Correction) -> None:
        """Record a confirmation, replacing any earlier one for the same chart title.

        Replacing rather than refusing is what makes a wrong correction recoverable: the
        admin fixes the row again and the new answer simply takes over.
        """
        stored = self._load()
        stored[normalized_title] = correction
        filestore.write_yaml(
            self._path,
            {BY_TITLE_KEY: {
                title: entry.model_dump() for title, entry in stored.items()
            }},
            schema_version=CORRECTIONS_SCHEMA_VERSION,
        )
