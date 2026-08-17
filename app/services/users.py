"""Single-admin account store and first-run bootstrap (Step 6, ruling #4).

There is exactly one account. On first run it is created with a random password
printed once to the container logs (`docker logs`) and `must_change_password`
set — a random secret beats a fixed default, which would be guessable during the
window before the admin's first login on an internet-exposed host. All HTTP
concerns live elsewhere so this is deterministically unit-testable.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core import filestore
from app.core.audit import AuditAction, AuditLog, get_audit_log

USER_SCHEMA_VERSION = 1
USER_FILENAME = "user.yml"
DEFAULT_USERNAME = "admin"
DEFAULT_DISPLAY_NAME = "Admin"
DEFAULT_EMAIL = "admin@example.com"

# The two looks the app ships. A closed set, checked before anything is written: the
# value ends up as a class on <html>, and the template compares against these rather than
# interpolating, so a corrupted user.yml can never put arbitrary text in the markup.
THEME_DARK = "dark"
THEME_LIGHT = "light"
THEMES = frozenset({THEME_DARK, THEME_LIGHT})

BOOTSTRAP_PASSWORD_LENGTH = 16
PASSWORD_MIN_LENGTH = 12
_PASSWORD_ALPHABET = string.ascii_letters + string.digits

_hasher = PasswordHasher()
# Verified against, never matched: gives a wrong USERNAME the same Argon2 cost as a wrong
# PASSWORD. Built by the same hasher as real passwords, so the two can never drift apart
# in time/memory cost. Hashed once at import (~50ms of startup), not per request.
_DUMMY_HASH = _hasher.hash("boxmedia-timing-equalizer")


class PasswordPolicyError(ValueError):
    """A proposed password does not meet the minimum policy."""


class InvalidThemeError(ValueError):
    """A theme was submitted that this build does not ship."""


@dataclass
class User:
    username: str
    display_name: str
    email: str
    password_hash: str
    must_change_password: bool
    password_changed_at: str | None
    # Which look this account sees. Defaulted rather than required, so every user.yml
    # written before the setting existed keeps loading — and keeps looking as it does
    # today. Additive with a default, so USER_SCHEMA_VERSION does not move, the same
    # argument MovieResult.total_gross documents.
    theme: str = THEME_DARK

    def to_document(self) -> dict[str, object]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "password_hash": self.password_hash,
            "must_change_password": self.must_change_password,
            "password_changed_at": self.password_changed_at,
            "theme": self.theme,
        }


def validate_password_policy(password: str) -> None:
    """Reject weak passwords. Kept deliberately simple: length + character mix."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {PASSWORD_MIN_LENGTH} characters"
        )
    if password.isalpha() or password.isdigit():
        raise PasswordPolicyError("password must mix letters and numbers")


def burn_password_check(password: str) -> None:
    """Constant-work no-op: verify against a throwaway hash so a wrong username costs
    the same time as a wrong password.

    Without it, login skips Argon2 entirely when the username doesn't match — measured
    at 48ms versus 0.0001ms — which tells an attacker the admin's username and undoes
    the point of being able to rename the account.
    """
    try:
        _hasher.verify(_DUMMY_HASH, password)
    except VerifyMismatchError:
        pass


def _validated_theme(value: object) -> str:
    """A stored theme, or dark for anything this build does not ship.

    Read-side tolerance on purpose: a hand-edited or future-build user.yml should render
    the default look, not refuse to load. The write side is strict — see `set_theme`.
    """
    return value if isinstance(value, str) and value in THEMES else THEME_DARK


def _generate_bootstrap_password() -> str:
    # Guaranteed to satisfy the policy (letters + at least one digit).
    while True:
        candidate = "".join(
            secrets.choice(_PASSWORD_ALPHABET) for _ in range(BOOTSTRAP_PASSWORD_LENGTH)
        )
        if any(character.isdigit() for character in candidate) and any(
            character.isalpha() for character in candidate
        ):
            return candidate


class UserStore:
    """Loads/saves the single admin account from `<config>/user.yml`."""

    def __init__(self, config_dir: Path, *, audit: AuditLog | None = None) -> None:
        self._path = config_dir / USER_FILENAME
        self._audit = audit
        # (identity, User) or None. One attribute, assigned in a single statement, so a
        # request thread can never observe a new user paired with a stale identity.
        self._cache: tuple[tuple[int, int, int], User] | None = None

    def exists(self) -> bool:
        return self._path.exists()

    def _identity(self) -> tuple[int, int, int]:
        """What makes this exact file contents — inode, mtime, size.

        Inode rather than mtime alone because both write paths replace the file rather
        than edit it: `filestore.atomic_write_bytes` renames a temp file over it, and a
        backup restore renames the whole `config/` directory into place. Either produces
        a new inode, which a coarse-granularity filesystem clock could otherwise hide.
        Nanoseconds, not `st_mtime`, to avoid float rounding.
        """
        stat = self._path.stat()
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def load(self) -> User:
        """The admin account, re-read only when `user.yml` has actually changed.

        Returns a COPY: `verify_password`, `set_password` and `update_profile` all mutate
        what they are handed before saving it. Sharing the cached instance would let a
        half-applied change — or one whose `save()` then failed — be served as if it had
        been persisted.
        """
        identity = self._identity()
        cached = self._cache
        if cached is not None and cached[0] == identity:
            return replace(cached[1])

        document = filestore.read_yaml(self._path, expected_version=USER_SCHEMA_VERSION)
        user = User(
            username=document["username"],
            display_name=document["display_name"],
            email=document["email"],
            password_hash=document["password_hash"],
            must_change_password=bool(document["must_change_password"]),
            password_changed_at=document.get("password_changed_at"),
            # `.get`, unlike the required fields above: an existing user.yml has no theme
            # key, and indexing it would fail the load — which happens inside the auth
            # gate on every request, so it would lock the admin out of their own install.
            theme=_validated_theme(document.get("theme")),
        )
        self._cache = (identity, user)
        return replace(user)

    def save(self, user: User) -> None:
        filestore.write_yaml(
            self._path, user.to_document(), schema_version=USER_SCHEMA_VERSION
        )
        # Explicit, though the new inode would invalidate it anyway: this store's
        # correctness must not depend on how filestore chooses to write.
        self._cache = None

    def bootstrap_if_missing(self) -> str | None:
        """Create the admin on first run. Returns the one-time password, else None."""
        if self.exists():
            return None
        password = _generate_bootstrap_password()
        user = User(
            username=DEFAULT_USERNAME,
            display_name=DEFAULT_DISPLAY_NAME,
            email=DEFAULT_EMAIL,
            password_hash=_hasher.hash(password),
            must_change_password=True,
            password_changed_at=None,
        )
        self.save(user)
        self._audit_log().record(AuditAction.ADMIN_BOOTSTRAPPED, actor=user.username)
        return password

    def _audit_log(self) -> AuditLog:
        return self._audit if self._audit is not None else get_audit_log()

    def verify_password(self, password: str) -> bool:
        user = self.load()
        try:
            _hasher.verify(user.password_hash, password)
        except VerifyMismatchError:
            return False
        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = _hasher.hash(password)
            self.save(user)
        return True

    def set_password(self, new_password: str, *, clear_forced_change: bool = True) -> None:
        validate_password_policy(new_password)
        user = self.load()
        user.password_hash = _hasher.hash(new_password)
        user.password_changed_at = datetime.now(UTC).isoformat()
        if clear_forced_change:
            user.must_change_password = False
        self.save(user)

    def set_theme(self, theme: str) -> None:
        """Store the account's theme. Strict: an unknown value is refused, never coerced.

        The read side defaults instead, so a file this build cannot understand still
        renders; here the caller is a form submission and a wrong value is a bug or an
        attack, not something to quietly accept.
        """
        if theme not in THEMES:
            raise InvalidThemeError(f"unknown theme: {theme!r}")
        user = self.load()
        user.theme = theme
        self.save(user)

    def update_profile(self, *, username: str, display_name: str, email: str) -> None:
        user = self.load()
        user.username = username
        user.display_name = display_name
        user.email = email
        self.save(user)
