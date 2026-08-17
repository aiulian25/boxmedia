"""Security activity — the read side of the audit log (Features.md F4).

`app.core.audit` calls the login record "the admin's only signal that they are under
attack", but nothing ever read it back, and the distroless runtime has no shell to open
the file with. This page surfaces the recent events and a 24-hour tally.

Note the values here are partly attacker-controlled: a failed sign-in records whatever
username was submitted. They are rendered through Jinja's autoescaping (never `| safe`)
and clipped to a sane length so a pathological value can't break the table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request

from app.core.audit import DEFAULT_TAIL_LIMIT, AuditAction
from app.web.deps import current_user, format_timestamp, parse_timestamp, render

router = APIRouter()

SECURITY_PATH = "/security"
NAV_KEY = "settings"  # it lives under Settings; keep that nav item highlighted
RECENT_WINDOW = timedelta(hours=24)
MAX_FIELD_LENGTH = 120
# Rows worth catching the eye. A failed unattended backup belongs here for the same
# reason a failed sign-in does: nobody was watching when it happened, and the cost of
# missing it is only discovered at the worst moment.
ALERT_ACTIONS = frozenset(
    {AuditAction.LOGIN_FAILURE, AuditAction.LOGIN_LOCKED, AuditAction.BACKUP_FAILED}
)
_SUMMARY_KEYS = ("ts", "action", "actor", "source_ip")


def _clip(value: object) -> str:
    text = str(value)
    return text if len(text) <= MAX_FIELD_LENGTH else f"{text[:MAX_FIELD_LENGTH]}…"


def _detail(event: dict) -> str:
    """Everything beyond the standard columns, as compact `key=value` text."""
    extras = [
        f"{key}={value}"
        for key, value in event.items()
        if key not in _SUMMARY_KEYS and value is not None
    ]
    return _clip(", ".join(extras)) if extras else ""


def _event_view(event: dict) -> dict:
    return {
        "time": format_timestamp(parse_timestamp(event.get("ts"))),
        "action": _clip(event.get("action", "")),
        "actor": _clip(event.get("actor") or "—"),
        "source_ip": _clip(event.get("source_ip") or "—"),
        "detail": _detail(event),
        "alert": event.get("action") in ALERT_ACTIONS,
    }


def _recent_counts(events: list[dict], now: datetime) -> dict[str, int]:
    cutoff = now - RECENT_WINDOW
    counts = {"success": 0, "failure": 0, "locked": 0}
    tally = {
        AuditAction.LOGIN_SUCCESS: "success",
        AuditAction.LOGIN_FAILURE: "failure",
        AuditAction.LOGIN_LOCKED: "locked",
    }
    for event in events:
        bucket = tally.get(event.get("action"))
        if bucket is None:
            continue
        moment = parse_timestamp(event.get("ts"))
        if moment is not None and moment >= cutoff:
            counts[bucket] += 1
    return counts


@router.get(SECURITY_PATH)
def security_page(request: Request) -> object:
    current_user(request)
    events = request.app.state.audit.tail(DEFAULT_TAIL_LIMIT)
    return render(
        request,
        "security.html",
        active_nav=NAV_KEY,
        events=[_event_view(event) for event in events],
        counts=_recent_counts(events, datetime.now(UTC)),
    )
