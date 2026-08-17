"""Session lifetime: absolute TTL, sliding idle window, sweep, and delete-all.

A fake clock (patching `_now`, the same seam `LoginRateLimiter` uses) keeps these
deterministic and instant — no sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.sessions import SessionStore

START = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def __call__(self) -> datetime:
        return self.now


def _store(clock: FakeClock, **kwargs: object) -> SessionStore:
    store = SessionStore(**kwargs)
    store._now = clock  # noqa: SLF001 — the injectable clock seam
    return store


def test_session_expires_after_absolute_ttl() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(hours=1))
    session_id = store.create("admin")

    clock.advance(timedelta(minutes=59))
    assert store.get(session_id) is not None
    clock.advance(timedelta(minutes=2))
    assert store.get(session_id) is None  # past the 1h TTL


def test_idle_window_slides_with_activity() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(hours=12), idle=timedelta(minutes=30))
    session_id = store.create("admin")

    # Touching the session every 20 minutes keeps it alive well past the idle window.
    for _ in range(5):
        clock.advance(timedelta(minutes=20))
        assert store.get(session_id) is not None

    clock.advance(timedelta(minutes=31))  # now idle longer than the window
    assert store.get(session_id) is None


def test_idle_never_extends_past_absolute_ttl() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(hours=1), idle=timedelta(minutes=30))
    session_id = store.create("admin")

    for _ in range(5):  # constant activity for over an hour
        clock.advance(timedelta(minutes=15))
        store.get(session_id)

    assert store.get(session_id) is None  # the 1h ceiling still wins


def test_expired_entries_are_swept() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(minutes=10))
    for index in range(5):
        store.create(f"admin{index}")
    assert len(store._sessions) == 5  # noqa: SLF001

    clock.advance(timedelta(minutes=11))
    store.sweep()
    assert store._sessions == {}  # noqa: SLF001


def test_create_sweeps_stale_entries() -> None:
    # A cookie never presented again would otherwise linger until restart.
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(minutes=10))
    store.create("admin")
    clock.advance(timedelta(minutes=11))

    fresh = store.create("admin")
    assert list(store._sessions) == [fresh]  # noqa: SLF001


def test_delete_all_invalidates_every_session() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(hours=12))
    first, second = store.create("admin"), store.create("admin")

    store.delete_all()
    assert store.get(first) is None
    assert store.get(second) is None


def test_notice_is_returned_once_then_cleared() -> None:
    clock = FakeClock()
    store = _store(clock, ttl=timedelta(hours=12))
    session_id = store.create("admin", notice={"at": "2026-08-14T10:00:00+00:00", "failed": 2})

    assert store.pop_notice(session_id)["failed"] == 2
    assert store.pop_notice(session_id) is None  # one-shot
    assert store.get(session_id) is not None  # the session itself survives


def test_pop_notice_tolerates_unknown_sessions() -> None:
    store = _store(FakeClock())
    assert store.pop_notice(None) is None
    assert store.pop_notice("not-a-session") is None
