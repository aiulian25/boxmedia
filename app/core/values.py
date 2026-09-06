"""Coercions for values that came from somebody else's JSON.

Radarr, Sonarr, TMDB, Trakt and every on-disk cache have to make the same decision
about the same malformed field, so the decision is made once, here, rather than beside
whichever reader needed it first. `core` because nothing above it may be imported by a
reader this low — these depend on nothing at all.

Both are deliberately strict about `bool`: it is a subclass of `int` in Python, so a
JSON `true` would otherwise arrive as the number 1 and be stored as an id, a count, or a
rating.
"""

from __future__ import annotations


def int_or_none(value: object) -> int | None:
    """A real integer, or None. `bool` is an int in Python and would become 0/1."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def float_or_none(value: object) -> float | None:
    """A real number, or None. `bool` is excluded for the same reason."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
