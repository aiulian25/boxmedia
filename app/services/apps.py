"""External-app (Radarr, Sonarr) connection store (Step 10, ruling #2, #3).

Connections are managed in the UI and encrypted at rest: the API key is only
ever written as an AES-GCM token (Step 4), never in plaintext. This is where a
stored credential first touches disk, so nothing here returns a decrypted key
except the explicit `decrypt_key`/`build_client` paths the pipeline and
Test-Connection button use.

Each connection carries a KIND. Radarr was the only one for a long time, so the
field is absent from every apps.yml written before now and loads as `radarr` —
an additive field with a default, which by this project's convention does not
bump the schema version. The read side coerces (`_validated_kind`), the write
side refuses an unknown value outright: the same split `users._validated_theme`
and `set_theme` use, for the same reason.

The listing and primary lookups default to Radarr rather than to every kind, and
that is deliberate. Both predate kinds, and every one of their callers — the Add
menus, the target caret, the "where does this film live" resolution, the
first-run check — means the Radarr ones. Defaulting to "all" would have made a
newly added Sonarr silently appear as somewhere to send a FILM. Pass `kind=None`
to list across kinds, which is what the Settings page wants and nothing else does.
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
from app.services.sonarr import SERIES_TYPES

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

KIND_KEY = "kind"
KIND_RADARR = "radarr"
KIND_SONARR = "sonarr"
APP_KINDS = (KIND_RADARR, KIND_SONARR)
# What each kind is called where a person reads it — the card tag, the Which-app
# radio, the error naming a credential. Mirrors mediaserver.SERVER_NAMES so the two
# service pickers read the same way.
KIND_NAMES = {KIND_RADARR: "Radarr", KIND_SONARR: "Sonarr"}
# What each kind manages, for the places that say it in a sentence rather than a tag.
KIND_MEDIA = {KIND_RADARR: "films", KIND_SONARR: "series"}

# Sonarr-only add options. A film has no seasons and no series type, so these are
# absent from every Radarr connection rather than stored as meaningless nulls.
# SERIES_TYPES itself lives in sonarr.py — it is that server's vocabulary, and the
# owning module holds the constant so there is one list to be wrong about.
SERIES_TYPE_KEY = "series_type"
SEASON_FOLDERS_KEY = "season_folders"
SEARCH_ON_ADD_KEY = "search_on_add"


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
    # Radarr or Sonarr. Defaulted rather than required so an apps.yml written before
    # kinds existed constructs exactly as it always did — see `_validated_kind`.
    kind: str = KIND_RADARR
    # Which connection the pipeline, library snapshot and add/upgrade actions use. Absent
    # from older apps.yml files, where the first connection is treated as primary. One
    # per KIND: a Radarr primary and a Sonarr primary answer different questions and
    # neither should unseat the other.
    primary: bool = False
    # What this connection adds a title as, when it is the chosen target. Per-connection
    # because Radarr and Sonarr assign profile ids per database and root folders are paths
    # on that host: a 1080p box and a 4K box share neither. None means "fall back to the
    # global Defaults", which is what every pre-existing apps.yml gets.
    quality_profile_id: int | None = None
    root_folder: str | None = None
    # Sonarr only, and None on every Radarr connection. None also means "not chosen yet"
    # for a Sonarr, so the add falls back to the same global default a fresh one uses.
    series_type: str | None = None
    season_folders: bool | None = None
    search_on_add: bool | None = None

    @property
    def kind_name(self) -> str:
        """Radarr / Sonarr, for anywhere a person reads it."""
        return KIND_NAMES[self.kind]

    def public(self) -> dict[str, object]:
        """View for templates — the key is masked, never revealed."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "api_key_mask": API_KEY_MASK,
            "kind": self.kind,
            "kind_name": self.kind_name,
            "primary": self.primary,
            "quality_profile_id": self.quality_profile_id,
            "root_folder": self.root_folder,
            "series_type": self.series_type,
            "season_folders": self.season_folders,
            "search_on_add": self.search_on_add,
        }


def _validated_kind(value: object) -> str:
    """A stored kind, or Radarr for anything this build does not ship.

    Read-side tolerance on purpose: an apps.yml from before kinds existed carries no
    kind at all and must load as what it is — a Radarr connection. A hand-edited or
    future-build file renders as Radarr rather than refusing to load. The write side is
    strict; see `add`.
    """
    return value if isinstance(value, str) and value in APP_KINDS else KIND_RADARR


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

    def list_apps(self, kind: str | None = KIND_RADARR) -> list[ExternalApp]:
        """Connections of one kind — Radarr unless asked otherwise.

        The default is not "everything" on purpose: see the module docstring. `kind=None`
        lists every connection, which only the Settings page wants.
        """
        apps = [
            ExternalApp(
                id=item["id"],
                name=item["name"],
                url=item["url"],
                api_key_encrypted=item["api_key_encrypted"],
                kind=_validated_kind(item.get(KIND_KEY)),
                primary=bool(item.get(PRIMARY_KEY, False)),
                quality_profile_id=item.get(QUALITY_PROFILE_KEY),
                root_folder=item.get(ROOT_FOLDER_KEY),
                series_type=item.get(SERIES_TYPE_KEY),
                season_folders=item.get(SEASON_FOLDERS_KEY),
                search_on_add=item.get(SEARCH_ON_ADD_KEY),
            )
            for item in self._load_raw()
        ]
        if kind is None:
            return apps
        return [app for app in apps if app.kind == kind]

    def get(self, app_id: str) -> ExternalApp:
        """One connection by id, whatever kind it is."""
        for app in self.list_apps(kind=None):
            if app.id == app_id:
                return app
        raise AppNotFoundError(app_id)

    def primary_id(self, kind: str = KIND_RADARR) -> str | None:
        """The connection every action of that kind uses: the one flagged primary, else
        the first configured of that kind. None when there is no connection of it."""
        apps = self.list_apps(kind)
        for app in apps:
            if app.primary:
                return app.id
        return apps[0].id if apps else None

    def set_primary(self, app_id: str) -> None:
        """Flag one connection primary, clearing the others OF ITS OWN KIND.

        Exactly one Radarr and one Sonarr win, independently: choosing which Sonarr adds
        a series has no business demoting the Radarr the weekly run talks to.
        """
        apps = self._load_raw()
        chosen = next((item for item in apps if item["id"] == app_id), None)
        if chosen is None:
            raise AppNotFoundError(app_id)
        kind = _validated_kind(chosen.get(KIND_KEY))
        for item in apps:
            if _validated_kind(item.get(KIND_KEY)) != kind:
                continue
            item[PRIMARY_KEY] = item["id"] == app_id
        self._save_raw(apps)
        self._audit.record(AuditAction.APP_UPDATED, app_id=app_id, kind=kind, primary=True)

    def add(self, *, name: str, url: str, api_key: str, kind: str = KIND_RADARR) -> ExternalApp:
        name = _validated_name(name)
        if not api_key.strip():
            raise InvalidAppError("API key is required")
        # Strict here, tolerant on read: the caller is a form submission, and a kind this
        # build does not ship is a bug or an attack, not something to quietly accept.
        if kind not in APP_KINDS:
            raise InvalidAppError(f"unknown app kind: {kind!r}")
        app = ExternalApp(
            id=f"{APP_ID_PREFIX}{secrets.token_hex(4)}",
            name=name,
            url=normalize_url(url),
            api_key_encrypted=crypto.encrypt_field(api_key.strip(), self._key),
            kind=kind,
        )
        apps = self._load_raw()
        apps.append(
            {
                "id": app.id,
                "name": app.name,
                "url": app.url,
                "api_key_encrypted": app.api_key_encrypted,
                KIND_KEY: app.kind,
            }
        )
        self._save_raw(apps)
        self._audit.record(AuditAction.APP_ADDED, app_id=app.id, name=app.name, kind=app.kind)
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
        # Removing the primary promotes the next connection OF THE SAME KIND, so the app
        # is never left pointing at a connection that no longer exists — and removing the
        # last Sonarr never hands the Sonarr primacy to a Radarr.
        removed = next(item for item in apps if item["id"] == app_id)
        kind = _validated_kind(removed.get(KIND_KEY))
        if removed.get(PRIMARY_KEY):
            successor = next(
                (item for item in remaining if _validated_kind(item.get(KIND_KEY)) == kind),
                None,
            )
            if successor is not None:
                successor[PRIMARY_KEY] = True
        self._save_raw(remaining)
        self._audit.record(AuditAction.APP_REMOVED, app_id=app_id, kind=kind)

    def set_defaults(
        self,
        app_id: str,
        *,
        quality_profile_id: int | None,
        root_folder: str | None,
        series_type: str | None = None,
        season_folders: bool | None = None,
        search_on_add: bool | None = None,
    ) -> None:
        """What this connection adds a title as when it is the chosen target.

        Stored on the connection rather than globally: the caller has already checked the
        profile id and folder against what THIS server reported, and neither value means
        anything on another instance.

        The last three are Sonarr's alone — a film has no seasons and no series type. They
        follow the blank-key-keeps-the-stored-one contract the rest of this file uses:
        None leaves whatever is stored, so a caller that knows nothing about them (every
        Radarr card, and the movie flows) cannot erase a Sonarr's settings. `False` is a
        value, not an absence, so a season-folders toggle turned off is honoured.
        """
        apps = self._load_raw()
        for item in apps:
            if item["id"] != app_id:
                continue
            if series_type is not None and series_type not in SERIES_TYPES:
                raise InvalidAppError(f"unknown series type: {series_type!r}")
            item[QUALITY_PROFILE_KEY] = quality_profile_id
            item[ROOT_FOLDER_KEY] = root_folder
            for key, value in (
                (SERIES_TYPE_KEY, series_type),
                (SEASON_FOLDERS_KEY, season_folders),
                (SEARCH_ON_ADD_KEY, search_on_add),
            ):
                if value is not None:
                    item[key] = value
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
        """A Radarr client for a stored Radarr connection.

        Refuses any other kind rather than building one anyway. Sonarr serves
        `/api/v3/system/status` too, so a Radarr client pointed at one would answer, the
        health dot would go green, and the first `movie` call would fail somewhere far
        from the cause. Better to be wrong loudly, here.
        """
        app = self.get(app_id)
        if app.kind != KIND_RADARR:
            raise InvalidAppError(
                f"{app.name} is a {app.kind_name} connection, not Radarr"
            )
        return client_for_credentials(
            app.url,
            self.decrypt_key(app_id),
            tls_verify=tls_verify,
            ca_file=ca_file,
            timeout=timeout,
        )
