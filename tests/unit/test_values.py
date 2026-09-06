"""Review step 11 unit test: the two coercions every reader now shares.

These used to be four private copies of one rule. Collapsing them means a change here
reaches Radarr, Sonarr, TMDB, Trakt and both on-disk caches at once, which is the point
of the move and also the reason the rule is worth pinning on its own.

The property that matters is the `bool` exclusion. `bool` is a subclass of `int`, so
without it a JSON `true` becomes the number 1 and is stored as an id, a count, or a
rating — wrong in a way nothing downstream can detect.
"""

from __future__ import annotations

import pytest

from app.core.values import float_or_none, int_or_none


@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_is_never_a_number(value: bool) -> None:
    assert int_or_none(value) is None
    assert float_or_none(value) is None


@pytest.mark.parametrize("value", [0, 1, -7, 121361])
def test_a_real_integer_survives_unchanged(value: int) -> None:
    assert int_or_none(value) == value
    assert float_or_none(value) == float(value)


@pytest.mark.parametrize("value", [None, "121361", "", [], {}, 1.5, object()])
def test_anything_that_is_not_an_integer_is_none(value: object) -> None:
    """A numeric STRING is refused too: these read other people's JSON, where a quoted id
    means the payload disagrees with its own schema, not that we should guess."""
    assert int_or_none(value) is None


@pytest.mark.parametrize("value", [7.5, -0.25, 0.0, 3])
def test_a_real_number_becomes_a_float(value: object) -> None:
    result = float_or_none(value)
    assert isinstance(result, float)
    assert result == float(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, "7.5", "", [], object()])
def test_anything_that_is_not_a_number_is_none(value: object) -> None:
    assert float_or_none(value) is None
