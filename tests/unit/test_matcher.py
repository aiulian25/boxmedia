"""Step 12 test: exhaustive table of normalization and matching cases."""

from __future__ import annotations

import pytest

from app.services.matcher import Candidate, find_match, normalize_title, titles_match


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("John Wick: Chapter 4", "John Wick 4"),
        ("Gladiator II", "Gladiator 2"),
        ("Dune: Part Two", "Dune Part 2"),
        ("Spider-Man: No Way Home", "Spider Man No Way Home"),
        ("The Batman", "Batman"),
        ("WALL·E", "WALL-E"),
        ("Amélie", "Amelie"),
        ("Fast & Furious", "Fast and Furious"),
        ("Mission: Impossible III", "Mission Impossible 3"),
    ],
)
def test_equivalent_titles_match(left: str, right: str) -> None:
    assert titles_match(left, right), f"{left!r} should match {right!r}"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Dune: Part Two", "Dune"),
        ("Gladiator II", "Gladiator"),
        ("Toy Story 3", "Toy Story 2"),
        ("Alien", "Aliens"),
    ],
)
def test_distinct_titles_do_not_match(left: str, right: str) -> None:
    assert not titles_match(left, right)


def test_se7en_normalizes_consistently() -> None:
    assert normalize_title("Se7en") == normalize_title("se7en")


def test_find_match_returns_payload() -> None:
    candidates = [
        Candidate("Dune: Part Two", 2024, 693134),
        Candidate("Oppenheimer", 2023, 872585),
    ]
    found = find_match("Dune Part 2", candidates)
    assert found.payload == 693134
    assert found.ambiguous is False
    assert find_match("Poor Things", candidates) is None


def test_find_match_year_disambiguates_remake() -> None:
    candidates = [
        Candidate("Nosferatu", 1922, 1001),
        Candidate("Nosferatu", 2024, 2024),
    ]
    assert find_match("Nosferatu", candidates, year=2024).payload == 2024
    assert find_match("Nosferatu", candidates, year=1922).payload == 1001
    assert find_match("Nosferatu", candidates, year=2024).ambiguous is False


def test_two_films_with_one_title_are_ambiguous_not_identified() -> None:
    """The audit's reproduction: first match won, and nothing downstream could tell."""
    found = find_match(
        "Nosferatu", [Candidate("Nosferatu", 1922, 1001), Candidate("Nosferatu", 2024, 2024)]
    )
    assert found.ambiguous is True


def test_a_year_that_matches_neither_leaves_it_ambiguous() -> None:
    found = find_match(
        "Nosferatu",
        [Candidate("Nosferatu", 1922, 1001), Candidate("Nosferatu", 2024, 2024)],
        year=1979,
    )
    assert found.ambiguous is True
