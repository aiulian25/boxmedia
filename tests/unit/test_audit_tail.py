"""Reading the audit log back: newest first, corruption-tolerant, bounded."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.audit import TAIL_READ_BYTES, AuditAction, AuditLog


def _log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def test_tail_returns_newest_first(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for index in range(3):
        log.record(AuditAction.LOGIN_SUCCESS, actor=f"admin{index}", source_ip="10.0.0.1")

    entries = log.tail()
    assert [entry["actor"] for entry in entries] == ["admin2", "admin1", "admin0"]


def test_tail_is_empty_when_no_log_exists(tmp_path: Path) -> None:
    assert _log(tmp_path).tail() == []


def test_tail_skips_a_torn_line(tmp_path: Path) -> None:
    # A crash mid-write leaves a partial line; the page must still render.
    log = _log(tmp_path)
    log.record(AuditAction.LOGIN_SUCCESS, actor="admin")
    with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-08-14T12:00:00+00:00", "action": "login_fai\n')
    log.record(AuditAction.LOGIN_FAILURE, actor="attacker")

    actions = [entry["action"] for entry in log.tail()]
    assert actions == ["login_failure", "login_success"]  # the torn line is dropped


def test_tail_respects_the_limit(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for index in range(10):
        log.record(AuditAction.LOGIN_SUCCESS, actor=f"admin{index}")
    assert len(log.tail(limit=4)) == 4


def test_tail_reads_only_the_end_of_a_large_log(tmp_path: Path) -> None:
    # Bigger than the tail window: the read is bounded and the first (cut) line dropped,
    # but the most recent entries still come back intact.
    path = tmp_path / "audit.jsonl"
    filler = json.dumps({"ts": "2026-08-01T00:00:00+00:00", "action": "pipeline_run",
                         "note": "x" * 200}) + "\n"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(filler * (TAIL_READ_BYTES // len(filler) + 50))
    log = AuditLog(path)
    log.record(AuditAction.LOGIN_FAILURE, actor="attacker", source_ip="10.0.0.9")

    entries = log.tail()
    assert entries[0]["action"] == "login_failure"
    assert entries[0]["actor"] == "attacker"
    assert all(isinstance(entry, dict) for entry in entries)
