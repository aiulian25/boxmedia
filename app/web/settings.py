"""Settings page: User Management (Step 8) + External Apps (Step 10).

Handlers act then redirect back with a fixed status code; the GET turns that code
into a banner. Test Connection exists so a wrong URL or key surfaces at config
time rather than failing silently inside a scheduled background run.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse

from app.core.audit import AuditAction
from app.core.filestore import dir_size_bytes
from app.services import boxoffice
from app.services.apps import (
    APP_KINDS,
    KIND_MEDIA,
    KIND_NAMES,
    KIND_RADARR,
    KIND_SONARR,
    MAX_APP_NAME_LENGTH,
    InvalidAppError,
)
from app.services.backup import (
    MAX_UPLOAD_BYTES,
    BackupCorruptError,
    BackupError,
    BackupInfo,
    BackupKeyError,
    BackupSchemaError,
)
from app.services.boxoffice import (
    MAX_CHART_SIZE,
    MIN_CHART_SIZE,
    REGIONS,
    spans_multiple_years,
    week_chip_label,
)
from app.services.discovery import (
    KEY_MASK,
    PROVIDER_NAMES,
    PROVIDER_TMDB,
    PROVIDER_TRAKT,
    PROVIDERS,
    DiscoveryError,
    ProbeResult,
    probe_tmdb,
    probe_trakt,
)
from app.services.filters import (
    DEFAULT_BACKUP_INTERVAL_DAYS,
    DEFAULT_BACKUP_KEEP,
    DEFAULT_CHART_SIZE,
    DEFAULT_REPORT_KEEP,
    DEFAULT_SCHEDULE_INTERVAL_HOURS,
    MAX_REPORT_KEEP,
    MIN_REPORT_KEEP,
    SCHEDULE_MODE_CADENCE,
    SCHEDULE_MODES,
    FiltersConfig,
)
from app.services.ignore import IgnoredMovie
from app.services.mediaserver import (
    KIND_PLEX,
    MEDIA_SERVER_KINDS,
    SERVER_NAMES,
    MediaServerAuthError,
    MediaServerError,
)
from app.services.mediaserver import (
    client_for_credentials as server_client_for_credentials,
)
from app.services.pipeline import SCRAPE_FAILURE_SUBDIR
from app.services.posters import (
    POSTER_SUBDIR,
    POSTER_WIDTH,
    SERIES_POSTER_WIDTH,
    sized,
)
from app.services.radarr import (
    RadarrAuthError,
    RadarrConnectionError,
    RadarrError,
    build_verify,
)
from app.services.radarr_options import RadarrOptions
from app.services.reports import Report
from app.services.sonarr import (
    APP_NAME as SONARR_APP_NAME,
)
from app.services.sonarr import (
    SERIES_TYPES,
    SonarrAuthError,
    SonarrConnectionError,
    SonarrError,
)
from app.web.deps import (
    MEDIA_SERVER_BACKOFF_KEY,
    client_ip,
    current_user,
    format_timestamp,
    load_all_radarr_options,
    load_all_sonarr_options,
    optional_int,
    radarr_backoff,
    radarr_client_for,
    radarr_client_for_credentials,
    refresh_series_library,
    render,
    sonarr_client_for,
    sonarr_client_for_credentials_dep,
)
from app.web.profile import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_USERNAME_LENGTH,
    STATUS_QUERY_KEY,
    ProfileStatus,
)

router = APIRouter()

SETTINGS_PATH = "/settings"
TEST_CREDENTIALS_PATH = "/settings/apps/test"
SERVER_TEST_CREDENTIALS_PATH = "/settings/media-server/test-credentials"
DISCOVERY_PATH = "/settings/discovery"
DISCOVERY_TEST_PATH = "/settings/discovery/test"
# Radarr names itself in /system/status. Sonarr and Lidarr answer the same shape, so
# without this an "it works" would be a lie about which app answered.
RADARR_APP_NAME = "radarr"
# A version is remote-supplied text on its way to a page: digits and dots only.
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){0,3}")
NAV_KEY = "settings"
# Short timeout for the on-load connection health probes so a dead Radarr doesn't
# stall the Settings page (improvement #8).
HEALTH_TIMEOUT_SECONDS = 4.0
BYTES_PER_KB = 1024
BYTES_PER_MB = 1024 * 1024


class AppHealth:
    OK = "ok"
    AUTH = "auth"
    UNREACHABLE = "unreachable"


class SettingsStatus:
    APP_ADDED_OK = "app_added_ok"
    APP_ADDED_AUTH = "app_added_auth"
    APP_ADDED_UNREACHABLE = "app_added_unreachable"
    APP_UPDATED = "app_updated"
    APP_REMOVED = "app_removed"
    APP_INVALID = "app_invalid"
    TEST_OK = "test_ok"
    TEST_AUTH = "test_auth"
    TEST_CONN = "test_conn"
    FILTERS_SAVED = "filters_saved"
    FILTERS_INVALID = "filters_invalid"
    REGION_SAVED = "region_saved"
    REGION_INVALID = "region_invalid"
    BACKUP_CREATED = "backup_created"
    BACKUP_DELETED = "backup_deleted"
    BACKUP_RESTORED = "backup_restored"
    BACKUP_IMPORTED = "backup_imported"
    BACKUP_FAILED = "backup_failed"
    BACKUP_VERIFIED = "backup_verified"
    # Three reasons an archive cannot be read, kept apart because the answer differs:
    # find the right key, upgrade, or reach for another archive. Still a closed enum —
    # the exception's own message is never shown, only mapped.
    BACKUP_BAD_KEY = "backup_bad_key"
    BACKUP_NEWER_SCHEMA = "backup_newer_schema"
    BACKUP_CORRUPT = "backup_corrupt"
    BACKUP_SCHEDULE_SAVED = "backup_schedule_saved"
    BACKUP_SCHEDULE_INVALID = "backup_schedule_invalid"
    UNIGNORED = "unignored"
    CLEANED = "cleaned"
    SERVER_SAVED = "server_saved"
    SERVER_INVALID = "server_invalid"
    SERVER_REMOVED = "server_removed"
    SERVER_TEST_OK = "server_test_ok"
    SERVER_TEST_AUTH = "server_test_auth"
    SERVER_TEST_CONN = "server_test_conn"
    SERVER_REFRESHED = "server_refreshed"
    # What the page reloads with after the one Save button has written every edited
    # card. A rejected card names its own status instead — "Settings saved" over a
    # value the server refused would be a lie.
    SETTINGS_SAVED = "settings_saved"
    SETTINGS_SAVE_FAILED = "settings_save_failed"
    SERVER_REFRESH_FAILED = "server_refresh_failed"
    DISCOVERY_SAVED = "discovery_saved"
    DISCOVERY_REMOVED = "discovery_removed"
    DISCOVERY_INVALID = "discovery_invalid"


_SUCCESS = "success"
_ERROR = "error"

STATUS_MESSAGES: dict[str, tuple[str, str]] = {
    ProfileStatus.PASSWORD_CHANGED: (_SUCCESS, "Password changed."),
    ProfileStatus.WRONG_CURRENT: (_ERROR, "Your current password is incorrect."),
    ProfileStatus.POLICY: (
        _ERROR,
        "New password does not meet the policy (12+ chars, letters + numbers).",
    ),
    ProfileStatus.MISMATCH: (_ERROR, "The new passwords do not match."),
    ProfileStatus.PROFILE_UPDATED: (_SUCCESS, "Profile updated."),
    ProfileStatus.INVALID_PROFILE: (_ERROR, "Please provide a display name and a valid email."),
    ProfileStatus.THEME_UPDATED: (_SUCCESS, "Theme updated."),
    SettingsStatus.APP_ADDED_OK: (_SUCCESS, "Connection added — Radarr responded."),
    SettingsStatus.APP_ADDED_AUTH: (
        _ERROR,
        "Connection added, but Radarr rejected the API key — fix the key on its card below.",
    ),
    SettingsStatus.APP_ADDED_UNREACHABLE: (
        _ERROR,
        "Connection added, but it did not answer — check the address, that Radarr is "
        "running, and TLS.",
    ),
    SettingsStatus.APP_UPDATED: (_SUCCESS, "Connection updated."),
    SettingsStatus.APP_REMOVED: (_SUCCESS, "Connection removed."),
    SettingsStatus.APP_INVALID: (_ERROR, "Please check the connection fields and try again."),
    SettingsStatus.TEST_OK: (_SUCCESS, "Connection succeeded — Radarr responded."),
    SettingsStatus.TEST_AUTH: (_ERROR, "Radarr rejected the API key."),
    SettingsStatus.TEST_CONN: (_ERROR, "Could not reach Radarr (check the address / TLS)."),
    SettingsStatus.FILTERS_SAVED: (_SUCCESS, "Radarr defaults saved."),
    SettingsStatus.FILTERS_INVALID: (_ERROR, "Please check the values and try again."),
    SettingsStatus.BACKUP_SCHEDULE_SAVED: (_SUCCESS, "Automatic backup schedule saved."),
    SettingsStatus.BACKUP_SCHEDULE_INVALID: (
        _ERROR,
        "Backup schedule needs whole numbers — days from 0, keep at least 1.",
    ),
    SettingsStatus.REGION_SAVED: (
        _SUCCESS,
        "Box office region saved — it applies to the next fetch.",
    ),
    SettingsStatus.SERVER_SAVED: (_SUCCESS, "Media server saved."),
    SettingsStatus.SERVER_INVALID: (_ERROR, "Please check the address and credential."),
    SettingsStatus.SERVER_REMOVED: (_SUCCESS, "Media server removed."),
    SettingsStatus.SERVER_TEST_OK: (
        _SUCCESS, "Connection succeeded — the server responded.",
    ),
    SettingsStatus.SERVER_TEST_AUTH: (_ERROR, "The server rejected the credential."),
    SettingsStatus.SERVER_TEST_CONN: (
        _ERROR, "Could not reach the media server (check the address / TLS).",
    ),
    SettingsStatus.SERVER_REFRESHED: (_SUCCESS, "Media server library refreshed."),
    SettingsStatus.SERVER_REFRESH_FAILED: (
        _ERROR,
        "Could not read the library — check the connection and try Test.",
    ),
    SettingsStatus.DISCOVERY_SAVED: (_SUCCESS, "Discovery keys saved."),
    SettingsStatus.DISCOVERY_REMOVED: (_SUCCESS, "Key deleted."),
    SettingsStatus.DISCOVERY_INVALID: (
        _ERROR,
        "That key could not be saved — check it and try again.",
    ),
    SettingsStatus.SETTINGS_SAVED: (_SUCCESS, "Settings saved."),
    SettingsStatus.SETTINGS_SAVE_FAILED: (
        _ERROR,
        "Something did not save — check the values and try again.",
    ),
    SettingsStatus.REGION_INVALID: (
        _ERROR,
        "That is not a region this build supports. Nothing was changed.",
    ),
    SettingsStatus.BACKUP_CREATED: (_SUCCESS, "Backup created."),
    SettingsStatus.BACKUP_DELETED: (_SUCCESS, "Backup deleted."),
    SettingsStatus.BACKUP_RESTORED: (_SUCCESS, "Backup restored — your previous data is back."),
    SettingsStatus.BACKUP_IMPORTED: (_SUCCESS, "Backup imported and restored."),
    SettingsStatus.BACKUP_FAILED: (_ERROR, "The backup operation failed. Nothing was changed."),
    # These four are read against a 3.4s toast, so they say the one thing that decides
    # what to do next and stop. Measured: the first drafts ran to 132 characters over four
    # lines, which is longer than the banner is on screen — and a longer translation of one
    # of those would have been unreadable outright.
    SettingsStatus.BACKUP_VERIFIED: (
        _SUCCESS,
        "Verified — this build can restore this archive.",
    ),
    # Names both causes deliberately. AES-GCM authenticates as it decrypts, so a failed
    # tag check cannot tell a wrong key from altered bytes; claiming the key alone would
    # send an admin hunting for a key when the file is simply damaged.
    SettingsStatus.BACKUP_BAD_KEY: (
        _ERROR,
        "Could not be unlocked — wrong key, or the file has been altered. "
        "Nothing was changed.",
    ),
    SettingsStatus.BACKUP_NEWER_SCHEMA: (
        _ERROR,
        "Made by a newer BoxMedia — upgrade to read it. Nothing was changed.",
    ),
    SettingsStatus.BACKUP_CORRUPT: (
        _ERROR,
        "Incomplete archive — a listed file is missing or fails its checksum. "
        "Nothing was changed.",
    ),
    SettingsStatus.UNIGNORED: (_SUCCESS, "Removed from your ignore list."),
    SettingsStatus.CLEANED: (_SUCCESS, "Cleanup complete."),
}


def _size_display(size_bytes: int) -> str:
    """Readable size for an archive or a /data area — megabytes, not thousands of KB."""
    if size_bytes >= BYTES_PER_MB:
        return f"{size_bytes / BYTES_PER_MB:.1f} MB"
    return f"{size_bytes / BYTES_PER_KB:.1f} KB"


def _backup_view(info: BackupInfo) -> dict:
    return {
        "name": info.name,
        "size": _size_display(info.size_bytes),
        "created": format_timestamp(info.created_at),
    }


# Which banner each specific failure earns. One table, so verify, restore and import can
# never explain the same exception differently — and anything without a specific reason
# still lands on the catch-all sentence it always did.
_BACKUP_FAILURE_STATUS = {
    BackupKeyError: SettingsStatus.BACKUP_BAD_KEY,
    BackupSchemaError: SettingsStatus.BACKUP_NEWER_SCHEMA,
    BackupCorruptError: SettingsStatus.BACKUP_CORRUPT,
}


def _backup_failure_status(error: BackupError) -> str:
    """The closed-enum code for a failure — never the exception's own text.

    Its message names paths and checksums and is written for the audit log; the banner is
    a fixed sentence chosen from the table above, which is what keeps status codes "a
    closed enum, never user text".
    """
    return _BACKUP_FAILURE_STATUS.get(type(error), SettingsStatus.BACKUP_FAILED)


def _redirect(request: Request, code: str) -> RedirectResponse:
    url_base = request.app.state.settings.url_base
    return RedirectResponse(
        url=f"{url_base}{SETTINGS_PATH}?{STATUS_QUERY_KEY}={code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(SETTINGS_PATH)
async def settings_page(request: Request) -> object:
    user = current_user(request)
    # Mark the EFFECTIVE primary, not just an explicitly flagged one: with no flag set
    # (a fresh install, or an apps.yml from before this existed) the first connection is
    # still the one in charge, and the badge has to say so. One per kind — which Radarr
    # the weekly run talks to and which Sonarr takes a series are separate answers.
    primary_by_kind = {kind: request.app.state.apps.primary_id(kind) for kind in APP_KINDS}
    # Each connection's own profiles/folders — both servers assign profile ids per
    # database, so one shared list would offer the wrong quality for another instance.
    # Both kinds are gathered together so the page costs one wall, not two in sequence.
    radarr_options, sonarr_options = await asyncio.gather(
        load_all_radarr_options(request), load_all_sonarr_options(request)
    )
    options_by_app = {**radarr_options, **sonarr_options}
    apps = []
    # The one caller that wants every kind: this page is where connections are managed,
    # so one invisible here would be one nobody could edit or remove.
    for app in request.app.state.apps.list_apps(kind=None):
        view = app.public()
        view["primary"] = app.id == primary_by_kind.get(app.kind)
        view["options"] = options_by_app.get(app.id, RadarrOptions())
        apps.append(view)
    filters = request.app.state.filters.load()
    backups = [_backup_view(info) for info in request.app.state.backups.list_backups()]
    # One read of the history for the ignored list's week chips. A completed-weeks scan
    # of a flat directory, next to the Radarr probes this page already awaits.
    ignored = _ignored_views(
        request.app.state.ignore.list_ignored(),
        request.app.state.reports.completed_weeks(),
    )
    health = await _connection_health(request, apps)
    # The global Radarr Defaults block: the RADARR primary's own profiles and folders,
    # never a Sonarr's — its profile ids come from a different database.
    options = options_by_app.get(primary_by_kind[KIND_RADARR]) or RadarrOptions()
    banner = STATUS_MESSAGES.get(request.query_params.get(STATUS_QUERY_KEY, ""))
    return render(
        request,
        "settings.html",
        active_nav=NAV_KEY,
        account=user,
        max_display_name_length=MAX_DISPLAY_NAME_LENGTH,
        max_app_name_length=MAX_APP_NAME_LENGTH,
        max_username_length=MAX_USERNAME_LENGTH,
        max_email_length=MAX_EMAIL_LENGTH,
        apps=apps,
        health=health,
        filters=filters,
        chart_size_bounds=(MIN_CHART_SIZE, MAX_CHART_SIZE),
        regions=REGIONS,
        server=(lambda stored: stored.public() if stored else None)(
            request.app.state.media_server.load()
        ),
        report_keep_bounds=(MIN_REPORT_KEEP, MAX_REPORT_KEEP),
        options=options,
        backups=backups,
        storage=_storage_view(request),
        snapshots=_snapshot_views(request),
        test_credentials_path=TEST_CREDENTIALS_PATH,
        server_test_credentials_path=SERVER_TEST_CREDENTIALS_PATH,
        # What is configured, never the values — a template that cannot reach a
        # secret cannot leak one.
        discovery=request.app.state.discovery.load().public(),
        discovery_path=DISCOVERY_PATH,
        discovery_test_path=DISCOVERY_TEST_PATH,
        ignored=ignored,
        app_kinds=[
            {"value": kind, "name": KIND_NAMES[kind], "media": KIND_MEDIA[kind]}
            for kind in APP_KINDS
        ],
        series_types=SERIES_TYPES,
        first_run=not apps,
        banner_kind=banner[0] if banner else None,
        banner_text=banner[1] if banner else None,
    )


def _weeks_of(entry: IgnoredMovie, reports: list[Report]) -> list[dict[str, object]]:
    """The weeks one ignored title charted in, in the order the reports are given.

    Matched by the entry's own identity rule, so an ignored 1970 original never borrows
    the weeks of a 2026 remake that normalizes to the same title.
    """
    weeks = [
        {"week": report.week, "rank": movie.rank, "report_id": report.id}
        for report in reports
        for movie in report.movies
        if entry.matches(movie.tmdb_id, movie.normalized_title)
    ]
    # Labelled per title, the same rule the search results apply: the year appears only
    # when this title's own weeks span more than one.
    with_year = spans_multiple_years([str(week["week"]) for week in weeks])
    for week in weeks:
        week["label"] = week_chip_label(str(week["week"]), with_year=with_year)
    return weeks


def _ignored_views(
    ignored: list[IgnoredMovie], reports: list[Report]
) -> list[dict[str, object]]:
    """Each ignored title with the weeks it charted in, newest week first.

    Shaped like the search results' weeks and rendered with the same chip, so a week
    reached from here and a week reached from a search are the same control.

    A title can legitimately have no weeks: the report it was ignored from may since have
    been deleted or pruned, and the entry still holds.
    """
    return [
        {
            "title": entry.title,
            "tmdb_id": entry.tmdb_id,
            "normalized_title": entry.normalized_title,
            "weeks": _weeks_of(entry, reports),
        }
        for entry in ignored
    ]


def _snapshot_views(request: Request) -> list[dict[str, str]]:
    """The stored scrape failures, newest first, as the Maintenance card lists them.

    The evidence has been write-only until now: the scraper saves the page it could not
    parse "for diagnosis", and the distroless runtime has no shell to open it with — so
    reading one meant `docker cp` from the host.
    """
    settings = request.app.state.settings
    return [
        {
            "name": name,
            "size": _size_display(size_bytes),
            "recorded": format_timestamp(datetime.fromtimestamp(mtime, UTC)),
        }
        for name, size_bytes, mtime in boxoffice.list_snapshots(
            settings.logs_dir / SCRAPE_FAILURE_SUBDIR
        )
    ]


def _storage_view(request: Request) -> list[dict[str, str]]:
    """What each /data area costs on disk, in the order the Storage card lists them.

    Four `iterdir` calls on flat directories — cheap enough to do on every Settings
    render, and the numbers are only useful when they're current.
    """
    settings = request.app.state.settings
    areas = (
        ("Posters", settings.cache_dir / POSTER_SUBDIR),
        ("Scrape snapshots", settings.logs_dir / SCRAPE_FAILURE_SUBDIR),
        ("Report history", settings.history_dir),
        ("Backups", settings.backups_dir),
    )
    return [{"label": label, "size": _size_display(dir_size_bytes(path))} for label, path in areas]


async def _connection_health(request: Request, apps: list[dict]) -> dict[str, str]:
    """Probe every configured connection concurrently; best-effort, never raises.

    Concurrently and not in sequence so the page costs one 4s wall in total rather than
    one per connection — the reason that mattered for two Radarrs matters more now that
    a Sonarr can sit beside them.
    """
    if not apps:
        return {}
    states = await asyncio.gather(
        *[_probe(request, app["id"], app.get("kind", KIND_RADARR)) for app in apps]
    )
    return {app["id"]: state for app, state in zip(apps, states, strict=True)}


async def _probe(request: Request, app_id: str, kind: str = KIND_RADARR) -> str:
    """Is this connection answering? Same 4s wall for both kinds.

    Kind-aware because the two build different clients — and because a Sonarr probed
    with a Radarr client would answer `/system/status` happily and go green, which is
    exactly the wrong-address mistake this dot exists to catch.
    """
    build = sonarr_client_for if kind == KIND_SONARR else radarr_client_for
    try:
        client = build(request, app_id, timeout=HEALTH_TIMEOUT_SECONDS)
        # Hard wall-time bound so even slow DNS on an unresolvable host can't stall
        # the Settings page (httpx's timeout does not cover name resolution).
        await asyncio.wait_for(client.system_status(), timeout=HEALTH_TIMEOUT_SECONDS)
    except (RadarrAuthError, SonarrAuthError):
        return AppHealth.AUTH
    except (
        RadarrConnectionError, RadarrError, SonarrConnectionError, SonarrError,
        TimeoutError, InvalidAppError,
    ):
        return AppHealth.UNREACHABLE
    return AppHealth.OK


# One place tying the post-save probe to the banner it produces.
_ADDED_STATUS = {
    AppHealth.OK: SettingsStatus.APP_ADDED_OK,
    AppHealth.AUTH: SettingsStatus.APP_ADDED_AUTH,
    AppHealth.UNREACHABLE: SettingsStatus.APP_ADDED_UNREACHABLE,
}


class TestResult:
    """Why a connection test ended the way it did. Wider than AppHealth because a form
    can carry an address that never becomes a request at all."""

    OK = "ok"
    AUTH = "auth"
    UNREACHABLE = "unreachable"
    BAD_URL = "bad_url"
    # "Something answered and spoke the API, but it is not the app you picked." The name
    # is historical — it now covers a Sonarr card pointed at a Radarr just as well.
    NOT_RADARR = "not_radarr"


@router.post(TEST_CREDENTIALS_PATH)
async def test_credentials(
    request: Request,
    url: str = Form(...),
    api_key: str = Form(...),
    kind: str = Form(KIND_RADARR),
) -> object:
    """Probe a connection the user has typed but not saved. Stores nothing.

    The credentials arrive on the same authenticated, CSRF-guarded POST that saving them
    would use, are held only for the length of this request, and are never written to
    disk or to the audit log. The reply is our own copy in every branch: nothing the
    remote host said is repeated back, since an error page from an unknown address is not
    text this app relays.

    One route for both kinds rather than a second one beside it: the contract, the
    bound, and the fragment are identical, and only which client gets built differs.
    """
    current_user(request)
    result = await _test_credentials(request, url, api_key, kind)
    return render(request, "_connection_test.html", result=result)


async def _test_credentials(
    request: Request, url: str, api_key: str, kind: str = KIND_RADARR
) -> dict[str, str | None]:
    # An unknown kind is not a reason to guess: the form only ever submits one of two,
    # so anything else is a bug or an attack and gets the same answer a bad address does.
    if kind not in APP_KINDS:
        return {"state": TestResult.BAD_URL, "version": None, "app_name": KIND_NAMES[KIND_RADARR]}
    app_name = KIND_NAMES[kind]
    result: dict[str, str | None] = {"state": TestResult.OK, "version": None, "app_name": app_name}
    try:
        build = (
            sonarr_client_for_credentials_dep
            if kind == KIND_SONARR
            else radarr_client_for_credentials
        )
        client = build(request, url, api_key, timeout=HEALTH_TIMEOUT_SECONDS)
        # Same hard wall-time bound as the on-load probes: httpx's own timeout does not
        # cover name resolution, so an unresolvable host would otherwise hold the request.
        status = await asyncio.wait_for(
            client.system_status(), timeout=HEALTH_TIMEOUT_SECONDS
        )
    except InvalidAppError:
        return {**result, "state": TestResult.BAD_URL}
    except (RadarrAuthError, SonarrAuthError):
        return {**result, "state": TestResult.AUTH}
    except (RadarrConnectionError, RadarrError, SonarrConnectionError, SonarrError, TimeoutError):
        return {**result, "state": TestResult.UNREACHABLE}
    if not _names_itself(status, kind):
        return {**result, "state": TestResult.NOT_RADARR}
    return {**result, "version": _version_of(status)}


def _names_itself(status: object, kind: str) -> bool:
    """Did the app that answered say it is the one the card is for?

    Radarr, Sonarr and Lidarr all answer `/system/status` with the same shape, so the
    name is the only thing distinguishing them — and pointing a Sonarr card at a Radarr
    is exactly as easy a mistake as the reverse. Without this the dot goes green and
    every later call fails somewhere confusing.
    """
    if not isinstance(status, dict):
        return False
    expected = SONARR_APP_NAME if kind == KIND_SONARR else RADARR_APP_NAME
    return str(status.get("appName", "")).strip().casefold() == expected


def _version_of(status: dict) -> str | None:
    """The remote version, only when it looks like one.

    Remote-supplied text that reaches a page, so it is matched against a strict shape
    rather than escaped and hoped for: digits and dots, nothing else, or it is dropped.
    """
    version = str(status.get("version", "")).strip()
    return version if _VERSION_RE.fullmatch(version) else None


@router.post("/settings/apps")
async def add_app(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    api_key: str = Form(...),
    kind: str = Form(KIND_RADARR),
) -> RedirectResponse:
    """Save a connection, then say whether it actually answers.

    The test is not a gate: a server that is switched off, or not built yet, is still
    worth configuring. But adding one that cannot work should never look like success,
    and this is the path that holds with JavaScript off, where the Add form's own Test
    button is not offered.

    `kind` defaults to Radarr so a form from before the radio existed — or one submitted
    with the field stripped — still adds what it always added. `add` itself refuses an
    unknown value, so nothing invented gets stored.
    """
    current_user(request)
    try:
        app = request.app.state.apps.add(name=name, url=url, api_key=api_key, kind=kind)
    except InvalidAppError:
        return _redirect(request, SettingsStatus.APP_INVALID)
    # A brand-new id cannot be backed off, but clearing is what keeps that true if an id
    # is ever reused — and it costs nothing.
    radarr_backoff(request).note_success(app.id)
    health = await _probe(request, app.id, app.kind)
    # A Sonarr that answered is worth reading now: waiting for the morning job would
    # leave Discover unable to say what you already own until tomorrow.
    if app.kind == KIND_SONARR and health == AppHealth.OK:
        await refresh_series_library(request, app.id)
    return _redirect(request, _ADDED_STATUS[health])


@router.post("/settings/apps/{app_id}")
def update_app(
    request: Request,
    app_id: str,
    name: str = Form(...),
    url: str = Form(...),
    api_key: str = Form(""),
    quality_profile_id: str = Form(""),
    root_folder: str = Form(""),
    series_type: str = Form(""),
    season_folders: str = Form(""),
    search_on_add: str = Form(""),
) -> RedirectResponse:
    """Everything about one connection, saved together.

    Identity and defaults share a route because they share a card: two Save buttons meant
    editing the name and the quality, pressing one, and silently losing the other.

    The defaults are vetted against what THIS server reported, so a profile id from
    another instance's database can never be stored against it.

    The last three are Sonarr's and arrive empty from every Radarr card, where the fields
    do not exist. `_validated_defaults` drops them for a Radarr connection rather than
    trusting the form not to send them.
    """
    current_user(request)
    defaults = _validated_defaults(
        request, app_id, quality_profile_id, root_folder,
        series_type=series_type, season_folders=season_folders, search_on_add=search_on_add,
    )
    if defaults is None:
        return _redirect(request, SettingsStatus.APP_INVALID)
    try:
        request.app.state.apps.update(app_id, name=name, url=url, api_key=api_key)
        request.app.state.apps.set_defaults(app_id, **defaults)
    except InvalidAppError:
        return _redirect(request, SettingsStatus.APP_INVALID)
    except KeyError:
        return _redirect(request, SettingsStatus.APP_INVALID)
    # Fixing the address or the key is the admin saying "try again" — waiting out the
    # backoff after a correction would look like the correction did not work.
    radarr_backoff(request).note_success(app_id)
    return _redirect(request, SettingsStatus.APP_UPDATED)


def _validated_defaults(
    request: Request,
    app_id: str,
    quality_profile_id: str,
    root_folder: str,
    *,
    series_type: str = "",
    season_folders: str = "",
    search_on_add: str = "",
) -> dict[str, object] | None:
    """The add-as defaults for one connection, or None when any value is one that
    connection is known NOT to offer.

    Vetted against the cached options only when there ARE cached options. With nothing
    cached — a connection added while the server was unreachable — the card renders plain
    number/text inputs instead of dropdowns, and refusing what is typed into them would
    make that fallback a dead end. An id we cannot check is not an id we know to be
    wrong; if it is, the add fails at the server and says so.
    """
    kind = _kind_of(request, app_id)
    state = request.app.state
    cache = state.sonarr_options if kind == KIND_SONARR else state.radarr_options
    options = cache.load(app_id)
    profile_id = optional_int(quality_profile_id)
    if (
        profile_id is not None
        and options.profiles
        and all(profile.id != profile_id for profile in options.profiles)
    ):
        return None
    folder = root_folder.strip() or None
    if folder is not None and options.root_folders and folder not in options.root_folders:
        return None
    defaults: dict[str, object] = {"quality_profile_id": profile_id, "root_folder": folder}
    if kind != KIND_SONARR:
        # A film has no seasons and no series type. Dropped here rather than trusted
        # not to arrive: the store would otherwise write them onto a Radarr connection
        # because a crafted form said so.
        return defaults
    chosen_type = series_type.strip() or None
    if chosen_type is not None and chosen_type not in SERIES_TYPES:
        return None
    defaults["series_type"] = chosen_type
    # Unchecked boxes are simply absent from a form submission, so "" is False here and
    # not "leave it alone" — the store's None-means-keep contract would make a toggle
    # impossible to turn off. The card always renders both, so both are always answered.
    defaults["season_folders"] = _checked(season_folders)
    defaults["search_on_add"] = _checked(search_on_add)
    return defaults


def _checked(value: str) -> bool:
    """A checkbox as submitted. Browsers send the value only when it is ticked."""
    return value.strip().lower() in ("on", "true", "1", "yes")


def _kind_of(request: Request, app_id: str) -> str:
    """What kind of server a stored connection is, or Radarr when there is no such id.

    Radarr for an unknown id so the caller behaves exactly as it did before kinds — the
    update itself then fails on the missing id and reports it.
    """
    try:
        return request.app.state.apps.get(app_id).kind
    except KeyError:
        return KIND_RADARR


@router.post("/settings/apps/{app_id}/delete")
def delete_app(request: Request, app_id: str) -> RedirectResponse:
    current_user(request)
    try:
        request.app.state.apps.remove(app_id)
        # Whichever cache holds it — forgetting from the wrong one would leave a
        # removed connection's profiles on disk forever.
        request.app.state.radarr_options.forget(app_id)
        request.app.state.sonarr_options.forget(app_id)
        request.app.state.series_cache.forget(app_id)
        radarr_backoff(request).forget(app_id)
    except KeyError:
        return _redirect(request, SettingsStatus.APP_INVALID)
    return _redirect(request, SettingsStatus.APP_REMOVED)


@router.post("/settings/apps/{app_id}/primary")
def make_primary(request: Request, app_id: str) -> RedirectResponse:
    """Choose which connection the pipeline and every add/upgrade action talks to."""
    current_user(request)
    try:
        request.app.state.apps.set_primary(app_id)
    except KeyError:
        return _redirect(request, SettingsStatus.APP_INVALID)
    return _redirect(request, SettingsStatus.APP_UPDATED)


@router.post("/settings/apps/{app_id}/test")
async def test_app(request: Request, app_id: str) -> RedirectResponse:
    """Probe a SAVED connection. Kind-aware, like the on-load health dots.

    It was not, and that was a real bug: `build_client` refuses a non-Radarr connection
    with InvalidAppError, which is a ValueError and matched none of the excepts below —
    so pressing Test on a Sonarr card raised a 500 instead of testing anything.
    """
    user = current_user(request)
    kind = _kind_of(request, app_id)
    build = sonarr_client_for if kind == KIND_SONARR else radarr_client_for
    try:
        client = build(request, app_id, timeout=HEALTH_TIMEOUT_SECONDS)
        # Same hard wall-time bound as the on-load probes: httpx's timeout does not cover
        # name resolution, so an unresolvable host would otherwise hold this request for
        # the OS resolver's timeout rather than for HEALTH_TIMEOUT_SECONDS.
        await asyncio.wait_for(client.system_status(), timeout=HEALTH_TIMEOUT_SECONDS)
    except (KeyError, InvalidAppError):
        return _redirect(request, SettingsStatus.APP_INVALID)
    except (RadarrAuthError, SonarrAuthError):
        _audit_test(request, user.username, app_id, "auth_failed")
        return _redirect(request, SettingsStatus.TEST_AUTH)
    except (
        RadarrConnectionError, RadarrError, SonarrConnectionError, SonarrError, TimeoutError
    ):
        _audit_test(request, user.username, app_id, "unreachable")
        return _redirect(request, SettingsStatus.TEST_CONN)
    _audit_test(request, user.username, app_id, "ok")
    # Pressing Test is the admin saying "try again now". A Sonarr that just answered is
    # worth re-reading, so a library fixed since the last failure shows up immediately
    # rather than at the next scheduled refresh.
    if kind == KIND_SONARR:
        await refresh_series_library(request, app_id)
    return _redirect(request, SettingsStatus.TEST_OK)


def _filters_with(stored: FiltersConfig, **changed: object) -> FiltersConfig:
    """A copy of the stored config with only the caller's fields replaced.

    Both filter forms write the whole file, so every field one form does not own has to
    be carried through. Doing that in one place means a field added to FiltersConfig
    later is carried by BOTH forms automatically, instead of being silently reset by
    whichever handler nobody remembered to update.

    Rebuilt through the model rather than `model_copy(update=...)`, which skips
    validation entirely — the bounds (chart_size <= MAX_CHART_SIZE, backup_keep >= 1)
    are what turn a bad submission into FILTERS_INVALID instead of a stored bad value.
    """
    unknown = set(changed) - set(FiltersConfig.model_fields)
    if unknown:
        # Pydantic ignores unknown keys, so a mistyped field name would "save"
        # successfully while changing nothing. TypeError is what Python raises for an
        # unexpected keyword argument, and the handlers already map it to a rejection.
        raise TypeError(f"not FiltersConfig fields: {sorted(unknown)}")
    return FiltersConfig(**{**stored.model_dump(), **changed})


def _validated_mode(mode: str) -> str:
    """The submitted schedule mode, or a ValueError the handler turns into a rejection."""
    if mode not in SCHEDULE_MODES:
        raise ValueError(f"unknown schedule mode: {mode!r}")
    return mode


def _server_client(request: Request, *, timeout: float | None = None):  # noqa: ANN202
    settings = request.app.state.settings
    return request.app.state.media_server.build_client(
        tls_verify=settings.outbound_tls_verify,
        ca_file=str(settings.tls_ca_file) if settings.tls_ca_file else None,
        timeout=timeout,
    )


@router.post("/settings/media-server")
async def save_media_server(
    request: Request,
    url: str = Form(""),
    token: str = Form(""),
    kind: str = Form(KIND_PLEX),
) -> RedirectResponse:
    """Create or update the one media server connection.

    A blank secret keeps the stored one, the Radarr cards' contract — but only for the
    same server; the store refuses to carry a Plex token over to Jellyfin. The stale
    library snapshot is dropped on save: pointing at a different address, or a
    different KIND of server, must not leave the old one's films decorating cards for
    up to a TTL.

    Write-strict on the kind, unlike the read-tolerant loader: a form is a deliberate
    act, and a server this build does not ship is a rejected submission.
    """
    user = current_user(request)
    try:
        # The store is write-strict about the kind, so an unknown one arrives here as
        # the same InvalidAppError a bad address does. Guarding it again in the route
        # would be a line no test could tell the absence of.
        request.app.state.media_server.save(
            url=url, token=token.strip() or None, kind=kind
        )
    except InvalidAppError:
        return _redirect(request, SettingsStatus.SERVER_INVALID)
    request.app.state.media_server_cache.forget()
    request.app.state.audit.record(
        AuditAction.MEDIA_SERVER_UPDATED, actor=user.username, source_ip=client_ip(request)
    )
    return _redirect(request, SettingsStatus.SERVER_SAVED)


# --- Discovery credentials (TV step 5) ---


@router.post(DISCOVERY_PATH)
def save_discovery_keys(
    request: Request,
    tmdb_key: str = Form(""),
    trakt_client_id: str = Form(""),
) -> RedirectResponse:
    """Save whichever of the two credentials was typed.

    Both fields on one card and one Save, like the connection cards: two Save buttons
    meant filling in both, pressing one, and silently losing the other. A blank field
    keeps what is stored — the contract every secret field in this app has — so editing
    one key never forces re-pasting the other. Clearing one is `remove_discovery_key`,
    its own button, because "leave it alone" and "delete it" must not be one gesture.
    """
    current_user(request)
    store = request.app.state.discovery
    try:
        store.save(PROVIDER_TMDB, tmdb_key)
        store.save(PROVIDER_TRAKT, trakt_client_id)
    except DiscoveryError:
        return _redirect(request, SettingsStatus.DISCOVERY_INVALID)
    return _redirect(request, SettingsStatus.DISCOVERY_SAVED)


@router.post(DISCOVERY_PATH + "/{provider}/delete")
def remove_discovery_key(request: Request, provider: str) -> RedirectResponse:
    """Forget one credential. The user ruling: these are theirs to remove at any time."""
    current_user(request)
    try:
        request.app.state.discovery.remove(provider)
    except DiscoveryError:
        return _redirect(request, SettingsStatus.DISCOVERY_INVALID)
    return _redirect(request, SettingsStatus.DISCOVERY_REMOVED)


@router.post(DISCOVERY_TEST_PATH)
async def test_discovery_key(
    request: Request,
    provider: str = Form(...),
    tmdb_key: str = Form(""),
    trakt_client_id: str = Form(""),
) -> object:
    """Probe one credential — the typed one if there is one, else the stored one.

    Same contract as the connection Test buttons: authenticated, CSRF-guarded, held for
    the length of this request, never written to disk or the audit log. Testing what has
    been TYPED is the point — it is how a key is checked before it is committed to disk,
    which is the right way round.

    The reply is our own copy in every branch. Nothing either API said is repeated back,
    and no message here is built from a URL: for TMDB that URL carries the key.
    """
    current_user(request)
    if provider not in PROVIDERS:
        # The card submits one of two. Anything else is a bug or an attack, and must not
        # become a probe of an arbitrary credential under a made-up label.
        return render(
            request,
            "_connection_test.html",
            result={"state": ProbeResult.UNREACHABLE, "version": None, "app_name": "Discovery"},
        )
    # Whichever field this button belongs to. Both arrive because both share the card's
    # one form — that is what lets a key be checked BEFORE it is committed to disk.
    typed = tmdb_key if provider == PROVIDER_TMDB else trakt_client_id
    secret = typed.strip()
    if not secret or secret == KEY_MASK:
        # Nothing typed: test what is stored, so the button still answers "does the key
        # I saved last week still work?".
        secret = request.app.state.discovery.decrypt(provider) or ""
    app_name = PROVIDER_NAMES[provider]
    if not secret:
        return render(
            request,
            "_connection_test.html",
            result={"state": ProbeResult.AUTH, "version": None, "app_name": app_name},
        )
    verify = build_verify(
        tls_verify=request.app.state.settings.outbound_tls_verify,
        ca_file=str(request.app.state.settings.tls_ca_file)
        if request.app.state.settings.tls_ca_file
        else None,
    )
    probe = probe_tmdb if provider == PROVIDER_TMDB else probe_trakt
    state = await probe(secret, verify=verify)
    return render(
        request,
        "_connection_test.html",
        result={"state": state, "version": None, "app_name": app_name},
    )


@router.post(SERVER_TEST_CREDENTIALS_PATH)
async def test_server_credentials_route(
    request: Request,
    url: str = Form(...),
    token: str = Form(""),
    kind: str = Form(KIND_PLEX),
) -> object:
    """Probe an address and secret the user has typed but not saved. Stores nothing.

    The Radarr side has had this since it existed; the media server shipped with only
    the after-saving test, which asked people to commit a credential to disk before
    they could learn whether it works. Same contract as `test_credentials`:
    authenticated, CSRF-guarded, held for the length of this request, never written to
    disk or the audit log, and every reply is our own copy rather than anything the
    remote said.

    A blank secret means the saved one — testing an address change without re-pasting
    it is the whole reason the field may be left empty — but only for the SAME kind of
    server, or the probe would send a Plex token to Jellyfin.
    """
    current_user(request)
    return render(
        request,
        "_server_test.html",
        result=await _test_server_credentials(request, url, token, kind),
        server_name=SERVER_NAMES.get(kind, SERVER_NAMES[KIND_PLEX]),
    )


async def _test_server_credentials(
    request: Request, url: str, token: str, kind: str
) -> dict[str, str]:
    settings = request.app.state.settings
    stored = request.app.state.media_server.load()
    reusable = stored is not None and stored.kind == kind
    if kind not in MEDIA_SERVER_KINDS:
        return {"state": TestResult.BAD_URL}
    if not token and not reusable:
        return {"state": TestResult.AUTH}
    try:
        secret = token or request.app.state.media_server.decrypt_token()
        client = server_client_for_credentials(
            url,
            secret,
            kind=kind,
            tls_verify=settings.outbound_tls_verify,
            ca_file=str(settings.tls_ca_file) if settings.tls_ca_file else None,
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
        sections = await asyncio.wait_for(
            client.movie_section_keys(), timeout=HEALTH_TIMEOUT_SECONDS
        )
    except InvalidAppError:
        return {"state": TestResult.BAD_URL}
    except MediaServerAuthError:
        return {"state": TestResult.AUTH}
    except (MediaServerError, TimeoutError):
        return {"state": TestResult.UNREACHABLE}
    # Reachable, authenticated, and no movie library: every later fetch would return
    # nothing and the cards would silently never mention Plex. That is a failed test.
    return {"state": TestResult.OK if sections else TestResult.NOT_RADARR}


@router.post("/settings/media-server/test")
async def test_media_server(request: Request) -> RedirectResponse:
    """Really try, every time — the backoff is for decorating pages, not for this."""
    user = current_user(request)
    if request.app.state.media_server.load() is None:
        return _redirect(request, SettingsStatus.SERVER_TEST_CONN)
    try:
        sections = await _server_client(request).movie_section_keys()
    except MediaServerAuthError:
        outcome = SettingsStatus.SERVER_TEST_AUTH
    except MediaServerError:
        outcome = SettingsStatus.SERVER_TEST_CONN
    else:
        outcome = SettingsStatus.SERVER_TEST_OK if sections else SettingsStatus.SERVER_TEST_CONN
    request.app.state.audit.record(
        AuditAction.MEDIA_SERVER_TESTED, actor=user.username, source_ip=client_ip(request),
        outcome=outcome,
    )
    return _redirect(request, outcome)


@router.post("/settings/media-server/refresh")
async def refresh_media_server(request: Request) -> RedirectResponse:
    """Fetch the library now, TTL be damned — the "I just added a film" button."""
    current_user(request)
    if request.app.state.media_server.load() is None:
        return _redirect(request, SettingsStatus.SERVER_REFRESH_FAILED)
    try:
        fetch = await _server_client(request).list_movies()
    except MediaServerError:
        return _redirect(request, SettingsStatus.SERVER_REFRESH_FAILED)
    request.app.state.media_server_cache.save(fetch)
    request.app.state.media_server_backoff.note_success(MEDIA_SERVER_BACKOFF_KEY)
    return _redirect(request, SettingsStatus.SERVER_REFRESHED)


@router.post("/settings/media-server/remove")
async def remove_media_server(request: Request) -> RedirectResponse:
    user = current_user(request)
    removed = request.app.state.media_server.remove()
    request.app.state.media_server_cache.forget()
    if removed:
        request.app.state.audit.record(
            AuditAction.MEDIA_SERVER_REMOVED, actor=user.username, source_ip=client_ip(request)
        )
    return _redirect(request, SettingsStatus.SERVER_REMOVED)


@router.post("/settings/region")
def save_region(request: Request, boxoffice_region: str = Form("")) -> RedirectResponse:
    """Which Box Office Mojo chart future runs fetch.

    Write-strict, unlike the model's own validator: a code this build does not ship is
    refused rather than quietly read as Domestic. The stored side is tolerant so a
    hand-edited file still starts; a form is a deliberate act and deserves an answer.

    Only the region is written — `_filters_with` carries every other field through, so
    saving this can never reset the schedule, the depth or the backup cadence.
    """
    current_user(request)
    if boxoffice_region not in REGIONS:
        return _redirect(request, SettingsStatus.REGION_INVALID)
    stored = request.app.state.filters.load()
    request.app.state.filters.save(
        _filters_with(stored, boxoffice_region=boxoffice_region)
    )
    return _redirect(request, SettingsStatus.REGION_SAVED)


@router.post("/settings/filters")
def save_filters(
    request: Request,
    schedule_interval_hours: str = Form(str(DEFAULT_SCHEDULE_INTERVAL_HOURS)),
    chart_size: str = Form(""),
    report_keep: str = Form(""),
    schedule_mode: str = Form(SCHEDULE_MODE_CADENCE),
) -> RedirectResponse:
    """The weekly box-office check: how often, and how deep.

    Quality and root folder are NOT here — they belong to a connection, and each one is
    edited on its own card. The stored global pair is carried through untouched: it is a
    read-only fallback for an install that only ever set the old shared values.
    """
    current_user(request)
    # This form owns the schedule and the depth; everything else is carried through, so
    # saving it can never reset the backup cadence or the legacy per-Radarr fallback.
    stored = request.app.state.filters.load()
    try:
        config = _filters_with(
            stored,
            schedule_interval_hours=int(schedule_interval_hours or DEFAULT_SCHEDULE_INTERVAL_HOURS),
            chart_size=int(chart_size or DEFAULT_CHART_SIZE),
            report_keep=int(report_keep or DEFAULT_REPORT_KEEP),
            # Write-strict, unlike the model's read-tolerant validator: a mode this build
            # does not ship is a rejected submission, not a silent fall back to the
            # default, because a form is a deliberate act.
            schedule_mode=_validated_mode(schedule_mode),
        )
    except (ValueError, TypeError):
        return _redirect(request, SettingsStatus.FILTERS_INVALID)

    request.app.state.filters.save(config)
    _reschedule_from_filters(request)
    return _redirect(request, SettingsStatus.FILTERS_SAVED)


@router.post("/settings/backups/schedule")
def save_backup_schedule(
    request: Request,
    backup_interval_days: str = Form(str(DEFAULT_BACKUP_INTERVAL_DAYS)),
    backup_keep: str = Form(""),
) -> RedirectResponse:
    """Automatic-backup cadence and retention — saved from the Backups section, where the
    admin is already looking at their archives."""
    current_user(request)
    stored = request.app.state.filters.load()
    try:
        config = _filters_with(
            stored,
            backup_interval_days=int(backup_interval_days or DEFAULT_BACKUP_INTERVAL_DAYS),
            backup_keep=int(backup_keep or DEFAULT_BACKUP_KEEP),
        )
    except (ValueError, TypeError):
        return _redirect(request, SettingsStatus.BACKUP_SCHEDULE_INVALID)

    request.app.state.filters.save(config)
    _reschedule_from_filters(request)
    return _redirect(request, SettingsStatus.BACKUP_SCHEDULE_SAVED)


@router.post("/settings/backups/create")
def create_backup(request: Request) -> RedirectResponse:
    current_user(request)
    # Honour the admin's configured retention — the service defaults to not pruning,
    # so a manual backup used to silently drop archives beyond the hard-coded 10.
    keep = request.app.state.filters.load().backup_keep
    try:
        request.app.state.backups.create(keep=keep, reason="manual")
    except BackupError:
        return _redirect(request, SettingsStatus.BACKUP_FAILED)
    return _redirect(request, SettingsStatus.BACKUP_CREATED)


@router.get("/settings/backups/{name}/download")
def download_backup(request: Request, name: str) -> object:
    current_user(request)
    try:
        path = request.app.state.backups.path_for(name)
    except BackupError:
        return _redirect(request, SettingsStatus.BACKUP_FAILED)
    return FileResponse(
        path, media_type="application/octet-stream", filename=path.name
    )


@router.post("/settings/backups/{name}/verify")
def verify_backup(request: Request, name: str) -> RedirectResponse:
    """Run the whole restore validation and report the outcome, changing nothing.

    Sync def so FastAPI runs it in the threadpool: decrypt, untar and checksum are the
    same CPU-heavy work a restore does, and the event loop should not be holding it.
    """
    user = current_user(request)
    try:
        request.app.state.backups.verify(name)
    except BackupError as exc:
        request.app.state.audit.record(
            AuditAction.BACKUP_FAILED,
            actor=user.username, source_ip=client_ip(request),
            reason="verify", name=Path(name).name, error=str(exc),
        )
        return _redirect(request, _backup_failure_status(exc))
    return _redirect(request, SettingsStatus.BACKUP_VERIFIED)


@router.post("/settings/backups/{name}/restore")
def restore_backup(request: Request, name: str) -> RedirectResponse:
    current_user(request)
    try:
        request.app.state.backups.restore_internal(name)
    except BackupError as exc:
        return _redirect(request, _backup_failure_status(exc))
    _reschedule_from_filters(request)
    return _redirect(request, SettingsStatus.BACKUP_RESTORED)


@router.post("/settings/backups/{name}/delete")
def delete_backup(request: Request, name: str) -> RedirectResponse:
    current_user(request)
    try:
        request.app.state.backups.delete(name)
    except BackupError:
        return _redirect(request, SettingsStatus.BACKUP_FAILED)
    return _redirect(request, SettingsStatus.BACKUP_DELETED)


@router.post("/settings/backups/import")
def import_backup(request: Request, backup_file: UploadFile) -> RedirectResponse:
    # Sync def so FastAPI runs the CPU-heavy restore (decrypt/untar/checksum/swap) in the
    # threadpool instead of on the event loop. Read at most one byte over the cap so an
    # oversized upload is rejected by restore_external without buffering it all in RAM.
    current_user(request)
    blob = backup_file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        request.app.state.backups.restore_external(blob)
    except BackupError as exc:
        return _redirect(request, _backup_failure_status(exc))
    _reschedule_from_filters(request)
    return _redirect(request, SettingsStatus.BACKUP_IMPORTED)


def _reschedule_from_filters(request: Request) -> None:
    """Push the saved schedule (chart interval + backup cadence) onto the live jobs."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return
    config = request.app.state.filters.load()
    scheduler.reschedule(
        config.schedule_interval_hours,
        schedule_mode=config.schedule_mode,
        backup_interval_days=config.backup_interval_days,
        backup_keep=config.backup_keep,
    )


def _audit_test(request: Request, username: str, app_id: str, result: str) -> None:
    request.app.state.audit.record(
        AuditAction.APP_TESTED,
        actor=username,
        source_ip=client_ip(request),
        app_id=app_id,
        result=result,
    )


@router.post("/settings/maintenance/prune-posters")
def prune_posters(request: Request) -> RedirectResponse:
    """Drop cached posters no retained report still references.

    The keep-set is derived here from the stored reports — the request carries no
    parameters, so nothing a client sends can widen what gets deleted.
    """
    user = current_user(request)
    # Through posters.sized() for the same reason the fetch is: the cache keys on the
    # URL, so a keep-set built from the raw URLs would mark every poster an orphan.
    keep = {
        sized(movie.poster_url, POSTER_WIDTH)
        for report in request.app.state.reports.list_reports()
        for movie in report.movies
        if movie.poster_url
    }
    # Series posters are kept by the same rule and at THEIR width. Without this every
    # press of Prune would wipe the artwork off every TV card — the keep-set is built
    # from the stored records, and a series is recorded in the library cache rather than
    # in a weekly report. A Discover row's poster is genuinely not kept: that shelf
    # re-fetches on its own TTL, so an orphan there costs one download, not a record.
    keep |= {
        sized(poster_url, SERIES_POSTER_WIDTH)
        for poster_url in request.app.state.series_cache.poster_urls()
    }
    removed = request.app.state.posters.prune(keep)
    request.app.state.audit.record(
        AuditAction.MAINTENANCE_PRUNE,
        actor=user.username, source_ip=client_ip(request),
        target="posters", removed=removed,
    )
    return _redirect(request, SettingsStatus.CLEANED)


@router.get("/settings/maintenance/snapshots/{name}")
def download_snapshot(request: Request, name: str) -> Response:
    """One stored scrape failure, as a download.

    text/plain, NEVER text/html. The file is a page from somewhere else that this app
    could not parse — a Box Office Mojo layout change, or whatever a proxy in front of it
    answered with — and rendering it in BoxMedia's own origin would run its scripts with
    this session's cookie. Four things stop that: the media type, the `filename=` that
    makes it an attachment, the `nosniff` the hardening middleware puts on every response
    so the type cannot be second-guessed, and the CSP that would refuse the scripts even
    then.

    A name that is not one this module writes is simply not found — the same answer the
    poster route gives, and the same answer a snapshot cleared in another tab gets.
    """
    current_user(request)
    settings = request.app.state.settings
    path = boxoffice.snapshot_path(settings.logs_dir / SCRAPE_FAILURE_SUBDIR, name)
    if path is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(path, media_type="text/plain", filename=path.name)


@router.post("/settings/maintenance/clear-snapshots")
def clear_snapshots(request: Request) -> RedirectResponse:
    """Delete the raw-HTML scrape debugging snapshots."""
    user = current_user(request)
    settings = request.app.state.settings
    removed = boxoffice.clear_snapshots(settings.logs_dir / SCRAPE_FAILURE_SUBDIR)
    request.app.state.audit.record(
        AuditAction.MAINTENANCE_PRUNE,
        actor=user.username, source_ip=client_ip(request),
        target="scrape_snapshots", removed=removed,
    )
    return _redirect(request, SettingsStatus.CLEANED)
