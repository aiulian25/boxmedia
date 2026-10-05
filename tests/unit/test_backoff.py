"""A connection that failed is left alone for minutes, not seconds."""

from __future__ import annotations

import pytest

from app.services.backoff import RadarrBackoff


def test_a_failure_is_honoured_for_five_minutes_not_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At one minute a dead box was still the slowest thing on the page: anyone browsing
    for longer paid its timeout again and again."""
    clock = [1000.0]
    monkeypatch.setattr("app.services.backoff.time.monotonic", lambda: clock[0])
    backoff = RadarrBackoff()
    backoff.note_failure("app-dead")

    clock[0] += 61  # past the old one-minute wait
    assert backoff.should_skip("app-dead")
    clock[0] += 240  # five minutes and a second in all
    assert not backoff.should_skip("app-dead")


def test_editing_a_connection_still_clears_the_wait_at_once() -> None:
    """A longer wait must not hide a fix: correcting the address is a reason to try now."""
    backoff = RadarrBackoff()
    backoff.note_failure("app-dead")

    backoff.note_success("app-dead")

    assert not backoff.should_skip("app-dead")
