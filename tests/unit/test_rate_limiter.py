"""Step 21 unit test: lockout after N failures, per-key isolation, reset."""

from __future__ import annotations

from app.core.security import MAX_TRACKED_KEYS, LoginRateLimiter

IP_A = "10.0.0.5"
IP_B = "10.0.0.9"


def test_locks_after_max_attempts() -> None:
    limiter = LoginRateLimiter(max_attempts=3)
    assert limiter.record_failure(IP_A) is False
    assert limiter.record_failure(IP_A) is False
    assert limiter.record_failure(IP_A) is True  # third failure locks
    assert limiter.is_locked(IP_A) is True


def test_lockout_is_per_key() -> None:
    limiter = LoginRateLimiter(max_attempts=2)
    limiter.record_failure(IP_A)
    limiter.record_failure(IP_A)
    assert limiter.is_locked(IP_A) is True
    # A different client (real IP via --proxy-headers) is unaffected.
    assert limiter.is_locked(IP_B) is False


def test_reset_clears_failures() -> None:
    limiter = LoginRateLimiter(max_attempts=3)
    limiter.record_failure(IP_A)
    limiter.reset(IP_A)
    assert limiter.record_failure(IP_A) is False  # counter started over
    assert limiter.is_locked(IP_A) is False


def test_failure_window_expires() -> None:
    # Failures older than window_seconds don't count toward a lockout: a fake clock lets
    # us cross the window without sleeping.
    clock = [0.0]
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=5)
    limiter._now = lambda: clock[0]
    assert limiter.record_failure(IP_A) is False  # 1st in the window
    assert limiter.record_failure(IP_A) is False  # 2nd
    clock[0] = 10.0  # past the 5s window -> the stale count resets
    assert limiter.record_failure(IP_A) is False  # counts as the 1st of a fresh window
    assert limiter.is_locked(IP_A) is False  # not locked despite three total failures


def test_expired_entries_purged_when_maps_grow() -> None:
    # Distinct IPs are otherwise never dropped; the opportunistic purge must bound the
    # map once it outgrows MAX_TRACKED_KEYS. A fake clock avoids real sleeping.
    clock = [0.0]
    limiter = LoginRateLimiter(window_seconds=1)
    limiter._now = lambda: clock[0]

    for index in range(MAX_TRACKED_KEYS + 500):
        limiter.record_failure(f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}")
    assert len(limiter._failures) == MAX_TRACKED_KEYS + 500

    clock[0] = 100.0  # advance well past the 1s window -> all prior entries are expired
    limiter.record_failure("172.16.0.1")
    assert len(limiter._failures) <= MAX_TRACKED_KEYS
