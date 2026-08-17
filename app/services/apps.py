"""External-app (Radarr) connection store (Step 10, ruling #2, #3).

Connections are managed in the UI and encrypted at rest: the API key is only
ever written as an AES-GCM token (Step 4), never in plaintext. Only Radarr-type
apps exist in v1 (ruling #3); the mockup's functionless "Username" field is
dropped. This is where a stored credential first touches disk, so nothing here
returns a decrypted key except the explicit `decrypt_key`/`build_client` paths
the pipeline and Test-Connection button use.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.core import crypto, filestore
from app.core.audit import AuditAction, AuditLog
from app.services.radarr import RadarrClient, build_verify

APPS_SCHEMA_VERSION = 1
APPS_FILENAME = "apps.yml"
APPS_KEY = "apps"
APP_ID_PREFIX = "app-"
PRIMARY_KEY = "primary"
API_KEY_MASK = "••••••••••••"  # shown in the UI; never the real key
# The name identifies the connection everywhere it matters — the Add button, the target
# menu, and the "In Library · <name>" badge — all inside a 208px poster card. Bounded at
# the input for the same reason display_name is (review Step 17), not merely truncated in
# CSS.
MAX_APP_NAME_LENGTH = 40
QUALITY_PROFILE_KEY = "quality_profile_id"
ROOT_FOLDER_KEY = "root_folder"
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class AppNotFoundError(KeyError):
    """No external app with the given id."""


class InvalidAppError(ValueError):
    """Submitted app fields are invalid (empty name, unparseable URL)."""


@dataclass(frozen=True)
class ExternalApp:
    id: str
    name: str
    url: str
    api_key_encrypted: str
    # Which connection the pipeline, library snapshot and add/upgrade actions use. Absent
    # from older apps.yml files, where the first connection is treated as primary.
    primary: bool = False
    # What this connection adds a film as, when it is the chosen target. Per-connection
    # because Radarr assigns profile ids per database and root folders are paths on that
    # host: a 1080p box and a 4K box share neither. None means "fall back to the global
    # Radarr Defaults", which is what every pre-existing apps.yml gets.
    quality_profile_id: int | None = None
    root_folder: str | None = None

    def public(self) -> dict[str, object]:
        """View for templates — the key is masked, never revealed."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "api_key_mask": API_KEY_MASK,
            "primary": self.primary,
            "quality_profile_id": self.quality_profile_id,
            "root_folder": self.root_folder,
        }


def _validated_name(raw: str) -> str:
    """A connection name that will fit where it is shown."""
    name = raw.strip()
    if not name:
        raise InvalidAppError("name is required")
    if len(name) > MAX_APP_NAME_LENGTH:
        raise InvalidAppError(
            f"name must be {MAX_APP_NAME_LENGTH} characters or fewer"
        )
    return name


def normalize_url(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise InvalidAppError("address is required")
    if not _SCHEME_RE.match(candidate):
        candidate = f"http://{candidate}"  # LAN Radarr is commonly plain http
    parsed = urlparse(candidate)
    if not parsed.netloc:
        raise InvalidAppError(f"could not parse address: {raw!r}")
    return candidate.rstrip("/")


def client_for_credentials(
    url: str,
    api_key: str,
    *,
    tls_verify: bool,
    ca_file: str | None,
    timeout: float | None = None,
) -> RadarrClient:
    """A Radarr client for credentials, stored or not.

    Testing a connection before it is saved has to talk to exactly what saving it would
    talk to, so the address goes through the same `normalize_url` that `add` applies —
    otherwise you could test `radarr.local:7878` and store something that resolves
    differently. Raises InvalidAppError for an address that cannot be parsed, which is the
    same answer `add` gives.
    """
    return RadarrClient(
        normalize_url(url),
        api_key.strip(),
        verify=build_verify(tls_verify=tls_verify, ca_file=ca_file),
        **({"timeout": timeout} if timeout is not None else {}),
    )


class AppsStore:
    def __init__(self, config_dir: Path, *, key: bytes, audit: AuditLog) -> None:
        self._path = config_dir / APPS_FILENAME
        self._key = key
        self._audit = audit

    def _load_raw(self) -> list[dict]:
        if not self._path.exists():
            return []
        document = filestore.read_yaml(self._path, expected_version=APPS_SCHEMA_VERSION)
        return list(document.get(APPS_KEY, []))

    def _save_raw(self, apps: list[dict]) -> None:
        filestore.write_yaml(self._path, {APPS_KEY: apps}, schema_version=APPS_SCHEMA_VERSION)

    def list_apps(self) -> list[ExternalApp]:
        return [
            ExternalApp(
                id=item["id"],
                name=item["name"],
                url=item["url"],
                api_key_encrypted=item["api_key_encrypted"],
                primary=bool(item.get(PRIMARY_KEY, False)),
                quality_profile_id=item.get(QUALITY_PROFILE_KEY),
                root_folder=item.get(ROOT_FOLDER_KEY),
            )
            for item in self._load_raw()
        ]

    def get(self, app_id: str) -> ExternalApp:
        for app in self.list_apps():
            if app.id == app_id:
                return app
        raise AppNotFoundError(app_id)

    def primary_id(self) -> str | None:
        """The connection every Radarr action uses: the one flagged primary, else the
        first configured. None when no connection exists."""
        apps = self.list_apps()
        for app in apps:
            if app.primary:
                return app.id
        return apps[0].id if apps else None

    def set_primary(self, app_id: str) -> None:
        """Flag one connection primary, clearing the others (exactly one wins)."""
        apps = self._load_raw()
        if not any(item["id"] == app_id for item in apps):
            raise AppNotFoundError(app_id)
        for item in apps:
            item[PRIMARY_KEY] = item["id"] == app_id
        self._save_raw(apps)
        self._audit.record(AuditAction.APP_UPDATED, app_id=app_id, primary=True)

    def add(self, *, name: str, url: str, api_key: str) -> ExternalApp:
        name = _validated_name(name)
        if not api_key.strip():
            raise InvalidAppError("API key is required")
        app = ExternalApp(
            id=f"{APP_ID_PREFIX}{secrets.token_hex(4)}",
            name=name,
            url=normalize_url(url),
            api_key_encrypted=crypto.encrypt_field(api_key.strip(), self._key),
        )
        apps = self._load_raw()
        apps.append(
            {
                "id": app.id,
                "name": app.name,
                "url": app.url,
                "api_key_encrypted": app.api_key_encrypted,
            }
        )
        self._save_raw(apps)
        self._audit.record(AuditAction.APP_ADDED, app_id=app.id, name=app.name)
        return app

    def update(self, app_id: str, *, name: str, url: str, api_key: str | None) -> ExternalApp:
        apps = self._load_raw()
        for item in apps:
            if item["id"] != app_id:
                continue
            # A rename flows straight through to the Add menu and the badges, which read
            # the live name on every render — so it is checked here too, not just on add.
            item["name"] = _validated_name(name) if name.strip() else item["name"]
            item["url"] = normalize_url(url)
            # A blank field means "leave the stored key unchanged".
            if api_key and api_key.strip() and api_key != API_KEY_MASK:
                item["api_key_encrypted"] = crypto.encrypt_field(api_key.strip(), self._key)
            self._save_raw(apps)
            self._audit.record(AuditAction.APP_UPDATED, app_id=app_id, name=item["name"])
            return self.get(app_id)
        raise AppNotFoundError(app_id)

    def remove(self, app_id: str) -> None:
        apps = self._load_raw()
        remaining = [item for item in apps if item["id"] != app_id]
        if len(remaining) == len(apps):
            raise AppNotFoundError(app_id)
        # Removing the primary promotes the next connection, so the app is never left
        # pointing at a connection that no longer exists.
        removed_primary = any(
            item["id"] == app_id and item.get(PRIMARY_KEY) for item in apps
        )
        if removed_primary and remaining:
            remaining[0][PRIMARY_KEY] = True
        self._save_raw(remaining)
        self._audit.record(AuditAction.APP_REMOVED, app_id=app_id)

    def set_defaults(
        self, app_id: str, *, quality_profile_id: int | None, root_folder: str | None
    ) -> None:
        """What this connection adds a film as when it is the chosen target.

        Stored on the connection rather than globally: the caller has already checked the
        profile id and folder against what THIS Radarr reported, and neither value means
        anything on another instance.
        """
        apps = self._load_raw()
        for item in apps:
            if item["id"] != app_id:
                continue
            item[QUALITY_PROFILE_KEY] = quality_profile_id
            item[ROOT_FOLDER_KEY] = root_folder
            self._save_raw(apps)
            self._audit.record(AuditAction.APP_UPDATED, app_id=app_id, defaults=True)
            return
        raise AppNotFoundError(app_id)

    def decrypt_key(self, app_id: str) -> str:
        return crypto.decrypt_field(self.get(app_id).api_key_encrypted, self._key)

    def build_client(
        self,
        app_id: str,
        *,
        tls_verify: bool,
        ca_file: str | None,
        timeout: float | None = None,
    ) -> RadarrClient:
        app = self.get(app_id)
        return client_for_credentials(
            app.url,
            self.decrypt_key(app_id),
            tls_verify=tls_verify,
            ca_file=ca_file,
            timeout=timeout,
        )
