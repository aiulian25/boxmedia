"""Initials for the nav avatar."""

from __future__ import annotations

import pytest

from app.web.deps import display_initials


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Robin Vale", "RV"),
        ("Admin", "A"),
        ("  jean-luc   picard  ", "JP"),  # extra whitespace collapses
        ("Ana Maria Ionescu", "AI"),  # first and LAST word, not the middle
        ("li", "L"),
        ("李明", "李"),  # scripts without case pass through unchanged
        ("ß", "S"),  # upper() expands to "SS"; the disc takes one character
        ("", ""),
        ("   ", ""),
    ],
)
def test_initials(display_name: str, expected: str) -> None:
    assert display_initials(display_name) == expected


def test_initials_never_exceed_two_characters() -> None:
    assert len(display_initials("Wolfgang Amadeus Mozart-Straße")) == 2
