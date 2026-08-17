"""Application configuration loaded from the environment / `.env` (ruling #2).

Only app-level secrets and infrastructure live here. External-app (Radarr)
connections are managed in the Settings UI and encrypted at rest, never here.

Loading fails fast: a missing session secret or an out-of-range port raises at
startup rather than letting the app run half-configured on an exposed host.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The limiter owns these defaults; importing them keeps the shipped policy and the
# configurable policy from drifting apart (core -> core, no cycle).
from app.core.security import (
    DEFAULT_LOCK_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_WINDOW_SECONDS,
)

ENV_PREFIX = "BM_"
SESSION_SECRET_MIN_LENGTH = 32
TCP_PORT_MIN = 1
TCP_PORT_MAX = 65535
DEFAULT_CONTAINER_PORT = 8686
DEFAULT_HOST_PORT = 58546
DEFAULT_SESSION_TTL_HOURS = 12
# Box Office Mojo weekly chart URL. Deliberately mirrored by the boxoffice scraper's
# BOM_WEEKLY_URL (kept in both layers) so core never imports the services layer.
DEFAULT_BOXOFFICE_URL = "https://www.boxofficemojo.com/weekly/"


def unwritable_data_dir_message(data_dir: Path) -> str:
    """What to tell someone whose data directory is not theirs to write.

    Names the uid that owns it, because that is the difference between the two causes:
    root means Docker made the directory and nothing prepared it, anything else means
    the app and its data belong to different users.
    """
    # Imported here rather than repeated, so the uid a user is told to chown to cannot
    # drift from the one the app actually runs as. Local import: provision is a
    # root-only entrypoint and has no business loading on every settings import.
    from app.core.provision import APP_UID

    try:
        owner = str(data_dir.stat().st_uid)
    except OSError:
        owner = "unknown"
    return (
        "\n" + "=" * 70 + "\n"
        f"  BoxMedia cannot write to {data_dir}.\n"
        "\n"
        f"  It runs as uid {APP_UID}, but that directory belongs to uid {owner}.\n"
        "  Docker creates a missing bind-mount directory as root, and this container\n"
        "  is unprivileged with a read-only filesystem, so it cannot fix that itself.\n"
        "\n"
        "  Using the published docker-compose.yml? The `init` service does this for\n"
        "  you on first run. Check that it is present, and that BM_DATA_PATH in .env\n"
        "  points at the same directory this service mounts — init preparing one path\n"
        "  while the app mounts another looks exactly like this.\n"
        "\n"
        "  Otherwise, on the host:\n"
        f"    sudo chown -R {APP_UID}:{APP_UID} <your data directory>\n"
        + "=" * 70 + "\n"
    )


class Settings(BaseSettings):
    """Validated application settings.

    Field names map to `BM_`-prefixed environment variables (e.g. the attribute
    `session_secret` is read from `BM_SESSION_SECRET`).
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required secrets/infra ---
    session_secret: str = Field(min_length=SESSION_SECRET_MIN_LENGTH)
    encryption_key_file: Path

    # --- Optional with defaults ---
    data_dir: Path = Path("/data")
    port: int = DEFAULT_CONTAINER_PORT
    host_port: int = DEFAULT_HOST_PORT
    url_base: str = ""
    # Secure cookies require HTTPS. Keep true in production: the reverse proxy
    # terminates TLS and (with uvicorn --proxy-headers) the app sees the https
    # scheme, so Secure cookies flow. Only integration tests over plain http set
    # this false.
    secure_cookies: bool = True
    # How long a signed-in session stays valid at most, and (optionally) how long it may
    # sit idle before expiring. 0 idle minutes = no idle timeout, absolute TTL only.
    session_ttl_hours: int = Field(default=DEFAULT_SESSION_TTL_HOURS, ge=1)
    session_idle_minutes: int = Field(default=0, ge=0)
    # Login lockout policy: tighten on an internet-exposed host, loosen on a LAN box.
    # The lower bounds stop a config typo from disabling brute-force protection.
    login_max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1)
    login_window_seconds: int = Field(default=DEFAULT_WINDOW_SECONDS, ge=10)
    login_lock_seconds: int = Field(default=DEFAULT_LOCK_SECONDS, ge=30)
    outbound_tls_verify: bool = True
    tls_ca_file: Path | None = None
    forwarded_allow_ips: str = "127.0.0.1"
    # The box-office chart source. Defaults to Box Office Mojo (ruling #6); override
    # if the upstream URL changes or to point tests at a fixture server.
    boxoffice_url: str = DEFAULT_BOXOFFICE_URL

    @field_validator("port", "host_port")
    @classmethod
    def _port_in_range(cls, value: int) -> int:
        if not (TCP_PORT_MIN <= value <= TCP_PORT_MAX):
            raise ValueError(
                f"port must be between {TCP_PORT_MIN} and {TCP_PORT_MAX}, got {value}"
            )
        return value

    @field_validator("tls_ca_file", mode="before")
    @classmethod
    def _blank_ca_file_is_none(cls, value: object) -> object:
        # An explicitly empty BM_TLS_CA_FILE arrives as "" and would otherwise coerce
        # to Path(".") — a directory — which then breaks SSL context creation on every
        # outbound Radarr call. Treat blank/whitespace as "no CA file".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("url_base")
    @classmethod
    def _normalize_url_base(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        if not stripped:
            return ""
        if not stripped.startswith("/"):
            raise ValueError("url_base must start with '/' (e.g. '/boxmedia') or be empty")
        return stripped

    # Derived runtime paths — one place so every module agrees on the layout.
    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_data_dirs(self) -> None:
        """Create the runtime directory layout if missing (idempotent).

        A PermissionError here is not a fault in the app — it is the data directory
        belonging to somebody else, which is far and away the most common way a first
        install goes wrong. Docker creates a missing bind-mount source as root, and this
        container runs unprivileged on a read-only filesystem, so it cannot repair that
        itself. What it CAN do is say so in words, instead of a forty-line traceback
        that names pathlib and blames nothing.
        """
        for directory in (
            self.config_dir,
            self.history_dir,
            self.logs_dir,
            self.cache_dir / "posters",
            self.backups_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                raise SystemExit(unwritable_data_dir_message(self.data_dir)) from None

    def public_summary(self) -> dict[str, object]:
        """Non-secret config for the config-check entrypoint and logs."""
        return {
            "data_dir": str(self.data_dir),
            "port": self.port,
            "host_port": self.host_port,
            "url_base": self.url_base or "(root)",
            "outbound_tls_verify": self.outbound_tls_verify,
            "tls_ca_file": str(self.tls_ca_file) if self.tls_ca_file else None,
            "encryption_key_file": str(self.encryption_key_file),
            "forwarded_allow_ips": self.forwarded_allow_ips,
            # Surfaced so `python -m app.core.config` proves the deployed session and
            # lockout policy is the one the .env intended. No secrets here.
            "session_ttl_hours": self.session_ttl_hours,
            "session_idle_minutes": self.session_idle_minutes,
            "login_max_attempts": self.login_max_attempts,
            "login_window_seconds": self.login_window_seconds,
            "login_lock_seconds": self.login_lock_seconds,
        }


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached after first load)."""
    return Settings()


def _main() -> int:
    """Config-check entrypoint: `python -m app.core.config`.

    Exits non-zero with a readable message when configuration is invalid, so a
    deploy can smoke-test its `.env` before starting the server.
    """
    import json
    import sys

    from pydantic import ValidationError

    try:
        settings = get_settings()
    except ValidationError as exc:
        print("BoxMedia configuration is invalid:\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            print(f"  - {ENV_PREFIX}{location.upper()}: {error['msg']}", file=sys.stderr)
        return 1
    print(json.dumps(settings.public_summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
