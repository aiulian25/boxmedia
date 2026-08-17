"""Title normalization and matching (Step 12) — the correctness heart.

A false negative re-adds a film the household already owns; a false positive
silently skips a genuine box-office hit. Everything here is a pure function of
its inputs (no I/O), so the behavior is pinned by an exhaustive table of cases.

Normalization folds the ways the same film is written differently across the
box-office chart and the Radarr library: Roman vs Arabic numerals, spelled-out
digits, a colon subtitle vs none, punctuation, diacritics, and articles.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Roman numerals up to a sensible sequel count; longer sequels don't exist.
_ROMAN_TO_ARABIC = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}
_WORD_TO_DIGIT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_ARTICLES = {"the", "a", "an"}
_PART_WORDS = {"part", "chapter", "vol", "volume"}


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _tokenize(text: str) -> list[str]:
    lowered = _strip_diacritics(text).lower()
    # Drop everything after a colon subtitle: "john wick: chapter 4" -> "john wick ... 4"
    # is handled by keeping the tokens but removing the colon; we keep subtitle tokens
    # because some films are known by them, but connector words are dropped below.
    lowered = lowered.replace("&", " and ")
    return re.findall(r"[a-z0-9]+", lowered)


def _canonical_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _tokenize(text):
        if token in _ARTICLES or token in _PART_WORDS:
            continue
        if token in _ROMAN_TO_ARABIC:
            tokens.append(str(_ROMAN_TO_ARABIC[token]))
            continue
        if token in _WORD_TO_DIGIT:
            tokens.append(str(_WORD_TO_DIGIT[token]))
            continue
        tokens.append(token)
    return tokens


def normalize_title(title: str) -> str:
    """A canonical, comparable form of a movie title."""
    return " ".join(_canonical_tokens(title))


def titles_match(left: str, right: str) -> bool:
    return normalize_title(left) == normalize_title(right)


@dataclass(frozen=True)
class Candidate:
    title: str
    year: int | None
    payload: Any


def find_match(
    title: str, candidates: list[Candidate], *, year: int | None = None
) -> Any | None:
    """Return the payload of the candidate whose title matches, else None.

    When several candidates share a normalized title (a remake — e.g. two
    "Nosferatu"), a supplied `year` selects the matching release; otherwise the
    first match wins.
    """
    target = normalize_title(title)
    matches = [c for c in candidates if normalize_title(c.title) == target]
    if not matches:
        return None
    if year is not None:
        for candidate in matches:
            if candidate.year == year:
                return candidate.payload
    return matches[0].payload
