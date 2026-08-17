"""Voluntary account management from the Settings page (Step 8, ruling #4).

Unlike the forced first-run change (Step 7), changing your password here requires
the current password. Handlers stay thin: they act, then redirect back to
Settings with a fixed status code the Settings page (Step 10) turns into a
banner. Status codes are a closed enum, never user text, so nothing is injected.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse

from app.core.audit import AuditAction
from app.services.users import InvalidThemeError, PasswordPolicyError
from app.web.deps import client_ip, current_user

router = APIRouter()

SETTINGS_PATH = "/settings"
LOGIN_PATH = "/login"
STATUS_QUERY_KEY = "status"
# The display name renders in the nav on every page, so it is bounded at the input
# rather than only truncated in CSS.
MAX_DISPLAY_NAME_LENGTH = 60
# The login name, stored and written into every audit entry as `actor`.
MAX_USERNAME_LENGTH = 60
# RFC 5321's maximum mailbox length.
MAX_EMAIL_LENGTH = 254


class ProfileStatus:
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 — status code, not a secret
    WRONG_CURRENT = "wrong_current"
    POLICY = "policy"
    MISMATCH = "mismatch"
    PROFILE_UPDATED = "profile_updated"
    INVALID_PROFILE = "invalid_profile"
    THEME_UPDATED = "theme_updated"


def _back_to_settings(request: Request, code: str) -> RedirectResponse:
    url_base = request.app.state.settings.url_base
    return RedirectResponse(
        url=f"{url_base}{SETTINGS_PATH}?{STATUS_QUERY_KEY}={code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/account/password")
def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    user = current_user(request)
    users = request.app.state.users
    audit = request.app.state.audit
    ip = client_ip(request)

    if not users.verify_password(current_password):
        audit.record(
            AuditAction.PASSWORD_CHANGE_REJECTED,
            actor=user.username, source_ip=ip, reason="wrong_current",
        )
        return _back_to_settings(request, ProfileStatus.WRONG_CURRENT)

    if new_password != confirm_password:
        return _back_to_settings(request, ProfileStatus.MISMATCH)

    try:
        users.set_password(new_password)
    except PasswordPolicyError:
        return _back_to_settings(request, ProfileStatus.POLICY)

    audit.record(AuditAction.PASSWORD_CHANGED, actor=user.username, source_ip=ip)
    return _back_to_settings(request, ProfileStatus.PASSWORD_CHANGED)


@router.post("/account/logout-all")
def logout_all_sessions(request: Request) -> RedirectResponse:
    """Invalidate every session, including this one — the post-incident 'sign out
    everywhere' action for a box whose cookie may have been copied."""
    user = current_user(request)
    request.app.state.sessions.delete_all()
    request.app.state.audit.record(
        AuditAction.SESSIONS_CLEARED, actor=user.username, source_ip=client_ip(request)
    )
    url_base = request.app.state.settings.url_base
    return RedirectResponse(
        url=f"{url_base}{LOGIN_PATH}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/account/theme")
def update_own_theme(request: Request, theme: str = Form(...)) -> RedirectResponse:
    """Switch this account between the shipped looks.

    The value is checked against a closed set before it is written, and the template
    compares against that set rather than interpolating it, so nothing a form sends can
    reach the markup. Lands back on Settings like every other action here.
    """
    user = current_user(request)
    try:
        request.app.state.users.set_theme(theme)
    except InvalidThemeError:
        return _back_to_settings(request, ProfileStatus.INVALID_PROFILE)
    request.app.state.audit.record(
        AuditAction.PROFILE_UPDATED,
        actor=user.username, source_ip=client_ip(request), theme=theme,
    )
    return _back_to_settings(request, ProfileStatus.THEME_UPDATED)


@router.post("/account/profile")
def update_own_profile(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    email: str = Form(...),
) -> RedirectResponse:
    user = current_user(request)
    username = username.strip()
    display_name = display_name.strip()
    email = email.strip()

    # The username is the login name — it must be present and space-free.
    if not username or " " in username or not display_name or "@" not in email:
        return _back_to_settings(request, ProfileStatus.INVALID_PROFILE)
    if (
        len(display_name) > MAX_DISPLAY_NAME_LENGTH
        or len(username) > MAX_USERNAME_LENGTH
        or len(email) > MAX_EMAIL_LENGTH
    ):
        return _back_to_settings(request, ProfileStatus.INVALID_PROFILE)

    request.app.state.users.update_profile(
        username=username, display_name=display_name, email=email
    )
    request.app.state.audit.record(
        AuditAction.PROFILE_UPDATED, actor=user.username, source_ip=client_ip(request)
    )
    return _back_to_settings(request, ProfileStatus.PROFILE_UPDATED)
