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

from collections.abc import Callable
from typing import TypeVar

Row = TypeVar("Row")


def int_or_none(value: object) -> int | None:
    """A real integer, or None. `bool` is an int in Python and would become 0/1."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def float_or_none(value: object) -> float | None:
    """A real number, or None. `bool` is excluded for the same reason."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class QueueProgress(dict[int, float]):
    """A download queue's progress by id, and whether that is the whole queue.

    Still a dict, because almost every reader only looks a film up. `complete` is for the one
    that reads ABSENCE as meaning something: with a longer queue than the one page asked for,
    a film that is not here may simply be further down.
    """

    complete: bool = True


def queue_is_complete(payload: dict, records_seen: int, page_size: int) -> bool:
    """Whether one page of a queue was all of it. A proxy may drop the total, and a full page
    with no total to say otherwise cannot be assumed to be the end."""
    total = int_or_none(payload.get("totalRecords"))
    return records_seen < page_size if total is None else total <= records_seen


def readable_rows(
    read: Callable[[dict], Row], rows: list, error: type[Exception]
) -> list[Row]:
    """Each row `read` can make sense of. One odd row must not cost a whole library, and a
    server's fields can hold anything, so what a reader trips over is the row's fault.

    Rows with none readable raise `error` instead: that is not an empty library, and a caller
    saving it as one would replace everything known about the real one.
    """
    readable: list[Row] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            readable.append(read(row))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    if rows and not readable:
        raise error("the server answered with rows that could not be read")
    return readable
