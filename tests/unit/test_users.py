"""Step 6 test: bootstrap hashes + flags, never stores plaintext, is idempotent."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.audit import AuditLog
from app.services.users import (
    PASSWORD_MIN_LENGTH,
    PasswordPolicyError,
    User,
    UserStore,
    burn_password_check,
    validate_password_policy,
)

STRONG_PASSWORD = "correcthorse7battery"


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return UserStore(tmp_path, audit=audit)


def test_bootstrap_creates_hashed_flagged_admin(store: UserStore, tmp_path: Path) -> None:
    password = store.bootstrap_if_missing()
    assert password is not None

    raw = yaml.safe_load((tmp_path / "user.yml").read_text())
    assert raw["username"] == "admin"
    assert raw["must_change_password"] is True
    # Plaintext bootstrap password is never on disk; only its argon2id hash is.
    assert password not in (tmp_path / "user.yml").read_text()
    assert raw["password_hash"].startswith("$argon2id$")


def test_bootstrap_is_idempotent(store: UserStore) -> None:
    first = store.bootstrap_if_missing()
    second = store.bootstrap_if_missing()
    assert first is not None
    assert second is None  # second start does not recreate or reveal a password


def test_verify_password_accepts_and_rejects(store: UserStore) -> None:
    password = store.bootstrap_if_missing()
    assert store.verify_password(password) is True
    assert store.verify_password("wrong-password-9") is False


def test_set_password_clears_forced_change(store: UserStore) -> None:
    store.bootstrap_if_missing()
    store.set_password(STRONG_PASSWORD)
    user = store.load()
    assert user.must_change_password is False
    assert user.password_changed_at is not None
    assert store.verify_password(STRONG_PASSWORD) is True


def test_bootstrap_password_satisfies_policy(store: UserStore) -> None:
    password = store.bootstrap_if_missing()
    validate_password_policy(password)  # must not raise


@pytest.mark.parametrize("weak", ["short1", "a" * PASSWORD_MIN_LENGTH, "1" * PASSWORD_MIN_LENGTH])
def test_policy_rejects_weak_passwords(weak: str) -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_policy(weak)


def test_update_profile_persists(store: UserStore) -> None:
    store.bootstrap_if_missing()
    store.update_profile(username="robin", display_name="Home Admin", email="me@home.lan")
    user = store.load()
    assert user.username == "robin"
    assert user.display_name == "Home Admin"
    assert user.email == "me@home.lan"


def test_burn_password_check_is_a_silent_no_op() -> None:
    # It exists purely to spend Argon2 time; it must never raise or return anything.
    assert burn_password_check("anything at all") is None


@pytest.mark.parametrize(
    "password",
    ["", "short", "ünïcodé-påsswörd", "x" * 4096, "\x00\x01binary"],
)
def test_burn_password_check_never_raises(password: str) -> None:
    # A login form can carry any bytes; an exception here would 500 the login page and
    # itself become a timing/behaviour oracle.
    assert burn_password_check(password) is None


def test_burn_password_check_uses_the_same_hasher_as_real_passwords(store: UserStore) -> None:
    """The equalizer only works if its cost matches a real verification.

    Pinned structurally rather than by timing: both hashes must carry the same Argon2
    parameters, which is what makes the two paths cost the same.
    """
    from app.services.users import _DUMMY_HASH

    store.bootstrap_if_missing()
    stored_hash = store.load().password_hash
    # argon2 encoded form: $argon2id$v=19$m=65536,t=3,p=4$...
    assert _DUMMY_HASH.split("$")[1:4] == stored_hash.split("$")[1:4]


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count real YAML reads so 'the cache works' is measured, not assumed."""
    from app.core import filestore

    calls = [0]
    original = filestore.read_yaml

    def counting(*args: object, **kwargs: object) -> dict:
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(filestore, "read_yaml", counting)
    return calls


def test_repeated_loads_parse_the_file_once(
    store: UserStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.bootstrap_if_missing()
    calls = _count_parses(monkeypatch)

    for _ in range(20):
        store.load()

    assert calls[0] == 1  # 19 parses avoided


def test_load_returns_a_copy_not_the_cached_instance(store: UserStore) -> None:
    """Callers mutate what load() hands them (set_password, update_profile,
    verify_password's rehash). A shared instance would let that corrupt the cache."""
    store.bootstrap_if_missing()
    store.load()  # populate the cache — the MISS path returns a copy of its own, so a
    first = store.load()  # mutation only reaches the cache via a HIT.
    first.username = "tampered"
    first.must_change_password = False

    second = store.load()
    assert second.username == "admin"
    assert second.must_change_password is True
    assert first is not second


def test_save_invalidates_the_cache(store: UserStore) -> None:
    store.bootstrap_if_missing()
    store.load()
    store.update_profile(username="renamed", display_name="New Name", email="a@b.co")
    assert store.load().username == "renamed"


def test_a_write_by_another_process_is_picked_up(
    store: UserStore, tmp_path: Path
) -> None:
    """A backup restore replaces config/ from outside this store — the cache must not
    keep serving the pre-restore account (and its old password hash)."""
    store.bootstrap_if_missing()
    cached = store.load()

    document = yaml.safe_load((tmp_path / "user.yml").read_text())
    document["username"] = "restored-admin"
    other = UserStore(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"))
    other.save(User(**{k: v for k, v in document.items() if k != "schema_version"}))

    assert cached.username == "admin"
    assert store.load().username == "restored-admin"


def test_forced_password_change_is_visible_immediately(store: UserStore) -> None:
    # The gate reads must_change_password on every request; a stale True would lock the
    # admin in the change-password loop forever, a stale False would skip it.
    store.bootstrap_if_missing()
    assert store.load().must_change_password is True
    store.set_password(STRONG_PASSWORD)
    assert store.load().must_change_password is False
