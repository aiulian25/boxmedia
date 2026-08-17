"""Login, logout, and the forced first-run password change (Step 7).

The forced-change page is what the gate redirects a freshly-bootstrapped admin
to; it does not require the current password because the user just authenticated
this session. The voluntary change (Step 8) is separate and does require it.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse

from app.core.audit import AuditAction, AuditLog
from app.core.config import Settings
from app.core.sessions import COOKIE_NAME
from app.services.users import PasswordPolicyError, burn_password_check
from app.web.deps import client_ip, current_user, render

router = APIRouter()

LOGIN_PATH = "/login"
CHANGE_PASSWORD_PATH = "/change-password"  # noqa: S105 — URL path, not a secret
DASHBOARD_PATH = "/dashboard"
SETTINGS_PATH = "/settings"
# How far back to look for the previous sign-in when building the post-login notice.
SIGN_IN_HISTORY_LIMIT = 500


def _redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"{request.app.state.settings.url_base}{path}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _sign_in_notice(audit: AuditLog) -> dict | None:
    """The previous successful sign-in and how many failures followed it.

    Walks the audit log newest-first, counting failed attempts until it reaches the last
    success — the pair of facts that tell an admin whether someone else has been trying
    (or has already got in). None on the very first sign-in, when there is nothing to
    compare against.
    """
    failures = 0
    for entry in audit.tail(SIGN_IN_HISTORY_LIMIT):
        action = entry.get("action")
        if action == AuditAction.LOGIN_SUCCESS:
            return {
                "at": entry.get("ts"),
                "ip": entry.get("source_ip"),
                "failed": failures,
            }
        if action == AuditAction.LOGIN_FAILURE:
            failures += 1
    return None


def _cookie_path(settings: Settings) -> str:
    """The session cookie's Path — the deployment's url_base, or root. Shared by set and
    delete so the two can never drift (a mismatch strands the cookie in the browser)."""
    return f"{settings.url_base}/" if settings.url_base else "/"


def _set_session_cookie(request: Request, response: RedirectResponse, session_id: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        path=_cookie_path(settings),
    )


@router.get(LOGIN_PATH)
def login_form(request: Request) -> object:
    if getattr(request.state, "user", None) is not None:
        return _redirect(request, DASHBOARD_PATH)
    # First-run hint: while the account still holds the bootstrap password, tell the
    # admin where to find the credentials. Cleared automatically after they change it.
    users = request.app.state.users
    first_run = users.exists() and users.load().must_change_password
    return render(request, "login.html", error=None, first_run=first_run)


@router.post(LOGIN_PATH)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> object:
    users = request.app.state.users
    audit = request.app.state.audit
    limiter = request.app.state.rate_limiter
    ip = client_ip(request)
    limiter_key = ip or "unknown"

    if limiter.is_locked(limiter_key):
        audit.record(AuditAction.LOGIN_LOCKED, actor=username, source_ip=ip)
        return render(
            request, "login.html", status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error="Too many failed attempts. Try again later.",
        )

    # Both branches must cost the same: verifying only when the username matches would
    # leak which half of the credential was wrong (see burn_password_check).
    account_matches = users.exists() and username == users.load().username
    if account_matches:
        ok = users.verify_password(password)
    else:
        burn_password_check(password)
        ok = False
    if not ok:
        audit.record(AuditAction.LOGIN_FAILURE, actor=username, source_ip=ip)
        if limiter.record_failure(limiter_key):
            audit.record(AuditAction.LOGIN_LOCKED, actor=username, source_ip=ip)
        return render(
            request, "login.html", status_code=status.HTTP_401_UNAUTHORIZED,
            error="Invalid username or password.",
        )

    limiter.reset(limiter_key)
    # Read the history BEFORE recording this sign-in, so "last sign-in" means the previous
    # one. Carried on the session, never through the URL: query strings end up in browser
    # history and access logs, and a crafted link could otherwise fake this banner.
    notice = _sign_in_notice(audit)
    session_id = request.app.state.sessions.create(username, notice=notice)
    audit.record(AuditAction.LOGIN_SUCCESS, actor=username, source_ip=ip)
    response = _redirect(request, DASHBOARD_PATH)
    _set_session_cookie(request, response, session_id)
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    session_id = request.cookies.get(COOKIE_NAME)
    request.app.state.sessions.delete(session_id)
    user = getattr(request.state, "user", None)
    request.app.state.audit.record(
        AuditAction.LOGOUT,
        actor=user.username if user else None,
        source_ip=client_ip(request),
    )
    response = _redirect(request, LOGIN_PATH)
    response.delete_cookie(COOKIE_NAME, path=_cookie_path(request.app.state.settings))
    return response


@router.get(CHANGE_PASSWORD_PATH)
def change_password_form(request: Request) -> object:
    current_user(request)  # gate guarantees a user; 401 otherwise
    return render(request, "change_password.html", error=None)


@router.post(CHANGE_PASSWORD_PATH)
def change_password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> object:
    user = current_user(request)
    users = request.app.state.users

    if new_password != confirm_password:
        return render(
            request, "change_password.html",
            status_code=status.HTTP_400_BAD_REQUEST, error="Passwords do not match.",
        )
    try:
        users.set_password(new_password)
    except PasswordPolicyError as exc:
        return render(
            request, "change_password.html",
            status_code=status.HTTP_400_BAD_REQUEST, error=str(exc),
        )

    request.app.state.audit.record(
        AuditAction.PASSWORD_CHANGED, actor=user.username, source_ip=client_ip(request)
    )
    # First-run mini-wizard: with no Radarr connection yet, send the admin straight
    # to Settings to add one rather than to an empty dashboard.
    if not request.app.state.apps.list_apps():
        return _redirect(request, SETTINGS_PATH)
    return _redirect(request, DASHBOARD_PATH)
