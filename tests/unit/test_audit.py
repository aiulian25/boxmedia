"""Step 5 test: valid JSONL, rotation, newline-injection escaping."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.audit import MAX_ACTOR_LENGTH, AuditAction, AuditLog


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_valid_jsonl(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(AuditAction.LOGIN_SUCCESS, actor="admin", source_ip="10.0.0.5")
    entries = _read_lines(tmp_path / "audit.jsonl")
    assert entries[0]["action"] == "login_success"
    assert entries[0]["actor"] == "admin"
    assert entries[0]["source_ip"] == "10.0.0.5"
    assert "ts" in entries[0]


def test_newline_in_field_cannot_forge_an_entry(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    # A crafted username with an embedded fake log line.
    log.record(AuditAction.LOGIN_FAILURE, actor='evil\n{"action":"login_success"}')
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # still exactly one physical line
    assert _read_lines(tmp_path / "audit.jsonl")[0]["action"] == "login_failure"


def test_rotation_keeps_bounded_history(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=200, keep=3)
    for index in range(50):
        log.record(AuditAction.PIPELINE_RUN, actor="scheduler", run=index)
    assert path.exists()
    assert (tmp_path / "audit.jsonl.1").exists()
    # Never more rotated files than keep-1.
    rotated = sorted(p.name for p in tmp_path.glob("audit.jsonl.*"))
    assert rotated == ["audit.jsonl.1", "audit.jsonl.2"]


def test_a_crafted_login_username_cannot_flood_the_audit_log(tmp_path: Path) -> None:
    """`actor` is whatever was typed into the login form — an UNAUTHENTICATED request.

    Rotation caps total size, so an unbounded value does not fill the disk; it rotates
    the real security history out of existence, which is worse. This log is described in
    its own docstring as the admin's only signal that they are under attack.
    """
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("login_failure", actor="x" * 50_000, source_ip="10.0.0.1")

    entry = log.tail(1)[0]
    assert len(entry["actor"]) == MAX_ACTOR_LENGTH + 1  # the clip plus its ellipsis
    assert entry["actor"].endswith("…")
    assert (tmp_path / "audit.jsonl").stat().st_size < 1024


def test_a_normal_actor_is_stored_untouched(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("login_success", actor="admin", source_ip="10.0.0.1")
    assert log.tail(1)[0]["actor"] == "admin"


def test_a_missing_actor_stays_none(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("pipeline_run")
    assert log.tail(1)[0]["actor"] is None
