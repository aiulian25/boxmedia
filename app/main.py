"""BoxMedia application factory and the auth gate (Step 7).

Wiring order matters and is fixed here: settings → data dirs → admin bootstrap →
service stores on `app.state` → the gate middleware → routers. The gate is the
one place that enforces "authenticated, and password already changed" before any
feature route runs, so it cannot be forgotten on an individual endpoint.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.core import crypto, security
from app.core.audit import AUDIT_FILENAME, AuditLog
from app.core.config import Settings, get_settings
from app.core.security import LoginRateLimiter
from app.core.sessions import COOKIE_NAME, SessionStore
from app.services.apps import AppsStore
from app.services.backfill import BackfillRunner
from app.services.backup import BackupService
from app.services.corrections import CorrectionStore
from app.services.filters import FiltersStore
from app.services.ignore import IgnoreStore
from app.services.pipeline import Pipeline
from app.services.plex import PlexLibraryCache, PlexStore
from app.services.posters import PosterCache
from app.services.radarr_options import RadarrOptionsCache
from app.services.release_ids import ReleaseIdCache
from app.services.reports import ReportsStore
from app.services.scheduler import BoxMediaScheduler
from app.services.users import UserStore
from app.web import auth, dashboard, deps, movies, profile, reports, security_page
from app.web import settings as settings_routes

HEALTH_PATH = "/health"
# Browsers probe this at the domain root regardless of url_base, so it is served from
# the root like /health rather than behind the prefix.
FAVICON_PATH = "/favicon.ico"
FAVICON_MEDIA_TYPE = "image/vnd.microsoft.icon"
FAVICON_FILENAME = "favicon.ico"
STATIC_MOUNT = "/static"
SEE_OTHER = 303
_STATIC_DIR = str(Path(__file__).resolve().parent / "static")
_VERSIONED_ASSETS = (("css", "app.css"), ("js", "app.js"), ("logo.png",), (FAVICON_FILENAME,))
ASSET_VERSION_LENGTH = 8


def _asset_version() -> str:
    """Fingerprint the served stylesheet and script so a rebuild gets a fresh URL.

    Without it the browser keeps serving its cached copies after a rebuild and renders the
    previous build's layout/behavior. Content-derived, so the URL only changes when an
    asset actually changes.
    """
    digest = hashlib.sha256()
    for relative_path in _VERSIONED_ASSETS:
        asset = Path(_STATIC_DIR).joinpath(*relative_path)
        try:
            digest.update(asset.read_bytes())
        except OSError:
            return __version__  # assets not built (dev/test) — fall back to the app version
    return digest.hexdigest()[:ASSET_VERSION_LENGTH]


def _print_bootstrap_banner(password: str) -> None:
    # Printed to stdout so it is visible via `docker logs` on first run only.
    print(
        "\n" + "=" * 70 + "\n"
        "  BoxMedia first-run admin account created.\n"
        "    username: admin\n"
        f"    temporary password: {password}\n"
        "  You must change this password at first login before anything else.\n"
        + "=" * 70 + "\n",
        flush=True,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_data_dirs()

    # Build the audit log from THIS settings object (composition root), not the
    # global singleton — so tests and multiple instances stay isolated.
    audit = AuditLog(settings.logs_dir / AUDIT_FILENAME)
    users = UserStore(settings.config_dir, audit=audit)
    bootstrap_password = users.bootstrap_if_missing()
    if bootstrap_password is not None:
        _print_bootstrap_banner(bootstrap_password)

    # Load the encryption key at startup — fail fast if it is missing or malformed,
    # rather than at the first attempt to save a Radarr API key.
    encryption_key = crypto.load_key(settings.encryption_key_file)

    @asynccontextmanager
    async def lifespan(running_app: FastAPI):
        config = running_app.state.filters.load()
        scheduler = BoxMediaScheduler(
            running_app.state.pipeline,
            interval_hours=config.schedule_interval_hours,
            schedule_mode=config.schedule_mode,
            backups=running_app.state.backups,
            backup_interval_days=config.backup_interval_days,
            backup_keep=config.backup_keep,
            audit=audit,
            reports=running_app.state.reports,
        )
        scheduler.start()
        running_app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown()
            # A backfill is an in-process task, so it dies with the loop either way;
            # cancelling says so at the next await instead of leaving the loop to be torn
            # down mid-week. Whole reports are already on disk; the rest is offered again.
            running_app.state.backfill.cancel()

    app = FastAPI(
        title="BoxMedia", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.audit = audit
    app.state.users = users
    app.state.apps = AppsStore(settings.config_dir, key=encryption_key, audit=audit)
    app.state.filters = FiltersStore(settings.config_dir, audit=audit)
    app.state.reports = ReportsStore(settings.history_dir)
    app.state.posters = PosterCache(settings.cache_dir)
    app.state.release_ids = ReleaseIdCache(settings.cache_dir)
    app.state.ignore = IgnoreStore(settings.config_dir, audit=audit)
    app.state.corrections = CorrectionStore(settings.config_dir)
    app.state.radarr_options = RadarrOptionsCache(settings.config_dir)
    # Per app instance, not global: a test's dead connection must not silence
    # another's.
    app.state.radarr_backoff = deps.RadarrBackoff()
    # The Plex trio: connection (token encrypted with the same key as the Radarr
    # ones), the on-disk library snapshot, and its own backoff so a down media server
    # cannot cost every render a timeout.
    app.state.plex = PlexStore(settings.config_dir, key=encryption_key, audit=audit)
    app.state.plex_cache = PlexLibraryCache(settings.cache_dir)
    app.state.plex_backoff = deps.RadarrBackoff()
    app.state.backups = BackupService(
        settings.data_dir, settings.backups_dir, key=encryption_key, audit=audit
    )
    app.state.pipeline = Pipeline(
        apps=app.state.apps,
        reports_store=app.state.reports,
        settings=settings,
        audit=audit,
        ignore_store=app.state.ignore,
        chart_size=lambda: app.state.filters.load().chart_size,
        report_keep=lambda: app.state.filters.load().report_keep,
        region=lambda: app.state.filters.load().boxoffice_region,
        corrections=app.state.corrections,
        release_ids=app.state.release_ids,
    )
    app.state.sessions = SessionStore(
        ttl=timedelta(hours=settings.session_ttl_hours),
        idle=(
            timedelta(minutes=settings.session_idle_minutes)
            if settings.session_idle_minutes
            else None
        ),
    )
    app.state.rate_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
        lock_seconds=settings.login_lock_seconds,
    )
    app.state.scheduler = None  # populated at Step 15
    app.state.backfill = BackfillRunner(app.state.pipeline)
    app.state.asset_version = _asset_version()

    app.mount(
        f"{settings.url_base}{STATIC_MOUNT}",
        StaticFiles(directory=_STATIC_DIR, check_dir=False),
        name="static",
    )

    _register_gate(app, settings)
    security.install(app)  # added after the gate → runs outermost (headers on all responses)

    # Every router carries the CSRF guard, so a new mutating route is protected by
    # construction rather than by remembering to add a check to its handler.
    csrf = [Depends(deps.csrf_guard)]
    app.include_router(auth.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(profile.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(settings_routes.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(reports.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(dashboard.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(movies.router, prefix=settings.url_base, dependencies=csrf)
    app.include_router(security_page.router, prefix=settings.url_base, dependencies=csrf)

    # HEAD as well as GET, same reason as the favicon below: uptime monitors commonly
    # probe with HEAD, and FastAPI's @app.get advertises GET only, so a HEAD check would
    # get 405 and report a healthy app as down. Docker's own healthcheck uses GET.
    @app.api_route(HEALTH_PATH, methods=["GET", "HEAD"])
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # HEAD as well as GET: browsers and caching proxies probe the icon with HEAD, and
    # FastAPI's @app.get advertises GET only (Starlette's StaticFiles does both).
    @app.api_route(FAVICON_PATH, methods=["GET", "HEAD"])
    def favicon() -> FileResponse:
        """The shipped icon, unauthenticated like /static — a browser probing the
        hardcoded root path must not bounce through the login redirect."""
        return FileResponse(
            Path(_STATIC_DIR) / FAVICON_FILENAME, media_type=FAVICON_MEDIA_TYPE
        )

    @app.get(f"{settings.url_base}/" if settings.url_base else "/")
    def root() -> RedirectResponse:
        return RedirectResponse(
            url=f"{settings.url_base}{dashboard.DASHBOARD_PATH}",
            status_code=SEE_OTHER,
        )

    return app


def _register_gate(app: FastAPI, settings: Settings) -> None:
    url_base = settings.url_base
    login_path = f"{url_base}/login"
    change_password_path = f"{url_base}/change-password"
    logout_path = f"{url_base}/logout"
    static_prefix = f"{url_base}{STATIC_MOUNT}/"

    def _redirect(path: str) -> RedirectResponse:
        return RedirectResponse(url=path, status_code=SEE_OTHER)

    @app.middleware("http")
    async def gate(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Exempt paths short-circuit BEFORE any session or account lookup: a page of 25
        # posters would otherwise take the session lock and read user.yml 25 extra times
        # for requests that never look at either. Downstream readers all use
        # `getattr(request.state, "user", None)`, so the unset attribute is safe.
        path = request.url.path
        if path in {HEALTH_PATH, FAVICON_PATH} or path.startswith(static_prefix):
            return await call_next(request)

        session = app.state.sessions.get(request.cookies.get(COOKIE_NAME))
        request.state.session = session
        request.state.user = (
            app.state.users.load() if session and app.state.users.exists() else None
        )

        if request.state.user is None:
            if path == login_path:
                return await call_next(request)
            return _redirect(login_path)

        must_change = request.state.user.must_change_password
        if must_change and path not in {change_password_path, logout_path}:
            return _redirect(change_password_path)

        return await call_next(request)
