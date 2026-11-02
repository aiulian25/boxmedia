"""Whole old state or whole restored state, whatever fails and whenever.

The crash matrix forks a real process and kills it with `os._exit` at each rename and sync in
turn, so nothing gets to clean up. That proves the logic at every boundary; it cannot prove
what a given NAS filesystem does across a power cut.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from app.core import filestore
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services import maintenance as maintenance_module
from app.services.background import SingleFlight
from app.services.backup import (
    BackupBusyError,
    BackupError,
    BackupRollbackError,
    BackupService,
)
from app.services.maintenance import (
    JOURNAL_FILENAME,
    MaintenanceBusyError,
    MaintenanceGate,
    RestoreRecoveryError,
    recover_interrupted_restore,
)
from app.services.users import UserStore
from app.web.settings import SettingsStatus
from tests.conftest import AppHarness, CsrfClient, build_harness
from tests.integration.test_backup import _make_settings, _seed_report

OLD_MARKER = "old state"
NEW_MARKER = "restored state"
MARKERS = ("config/marker.txt", "history/marker.txt", "logs/marker.txt", "cache/marker.txt")
CRASH_EXIT_CODE = 9
SECOND_PASSWORD = "second9password"
WAIT_SECONDS = 5.0
# Far past the end of any swap, so the sweep below knows it has seen every boundary.
MAX_CRASH_POINTS = 60


def _tree_hash(data_dir: Path) -> str:
    """Every store a restore replaces, plus any leftovers beside them."""
    digest = hashlib.sha256()
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(data_dir)
        if relative.parts[0] == "backups" or path.name == "audit.jsonl":
            continue
        digest.update(str(relative).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_markers(data_dir: Path, text: str) -> None:
    for marker in MARKERS:
        (data_dir / marker).parent.mkdir(parents=True, exist_ok=True)
        (data_dir / marker).write_text(text, encoding="utf-8")


def _service(
    settings: Settings, key: bytes, maintenance: MaintenanceGate | None = None
) -> BackupService:
    audit = AuditLog(settings.logs_dir / "audit.jsonl")
    return BackupService(
        settings.data_dir, settings.backups_dir, key=key, audit=audit, maintenance=maintenance
    )


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Settings, BackupService, str, str, str]:
    """An archive of one state, with a different state live. Both hashes for comparing."""
    settings, key = _make_settings(tmp_path, "data")
    _seed_report(settings)
    _write_markers(settings.data_dir, NEW_MARKER)
    service = _service(settings, key)
    name = service.create()
    restored = _tree_hash(settings.data_dir)
    _write_markers(settings.data_dir, OLD_MARKER)
    return settings, service, name, restored, _tree_hash(settings.data_dir)


def _assert_whole(data_dir: Path, expected: str) -> None:
    assert _tree_hash(data_dir) == expected
    assert not (data_dir / JOURNAL_FILENAME).exists()
    assert list(data_dir.glob(".*.retired-*")) == []
    assert list(data_dir.glob(".*-staging-*")) == []


# --- a process that dies part-way ---


def _crash_at(monkeypatch: pytest.MonkeyPatch, crash_point: int) -> None:
    """Replace each durability step so the Nth one kills the process, as a power cut would."""
    seen = [0]

    def counting(real):  # noqa: ANN001, ANN202
        def step(*args: object, **kwargs: object) -> object:
            seen[0] += 1
            if seen[0] == crash_point:
                os._exit(CRASH_EXIT_CODE)
            return real(*args, **kwargs)

        return step

    monkeypatch.setattr(os, "sync", counting(os.sync))
    monkeypatch.setattr(os, "replace", counting(os.replace))
    monkeypatch.setattr(os, "fsync", counting(os.fsync))
    real_rename = Path.rename

    def rename(self: Path, target: object) -> object:
        seen[0] += 1
        if seen[0] == crash_point:
            os._exit(CRASH_EXIT_CODE)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def _restore_and_exit(
    service: BackupService, name: str, monkeypatch: pytest.MonkeyPatch, crash_point: int
) -> None:
    """The forked child: restore with a kill armed, and never return to pytest."""
    real_swap = service._swap_in

    def swap_with_crash(staging: Path) -> None:
        _crash_at(monkeypatch, crash_point)
        real_swap(staging)

    service._swap_in = swap_with_crash  # type: ignore[method-assign]
    try:
        service.restore_internal(name)
    finally:
        os._exit(0)


def test_a_crash_at_any_boundary_recovers_to_one_whole_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forked per boundary, from a fresh copy of the same starting state each time. Only the
    swap counts: the safety backup ran first and has nothing of its own to undo."""
    outcomes: list[str] = []
    swept_past_the_end = False

    for crash_point in range(1, MAX_CRASH_POINTS):
        settings, key = _make_settings(tmp_path / str(crash_point), "data")
        _seed_report(settings)
        _write_markers(settings.data_dir, NEW_MARKER)
        service = _service(settings, key)
        name = service.create()
        restored = _tree_hash(settings.data_dir)
        _write_markers(settings.data_dir, OLD_MARKER)
        original = _tree_hash(settings.data_dir)

        child = os.fork()
        if child == 0:
            _restore_and_exit(service, name, monkeypatch, crash_point)

        _, status = os.waitpid(child, 0)
        crashed = os.waitstatus_to_exitcode(status) == CRASH_EXIT_CODE

        recover_interrupted_restore(settings.data_dir)

        state = _tree_hash(settings.data_dir)
        assert state in {original, restored}, f"mixed state after crash point {crash_point}"
        _assert_whole(settings.data_dir, state)
        assert UserStore(settings.config_dir, audit=service._audit).exists()
        if not crashed:
            assert state == restored
            swept_past_the_end = True
            break
        outcomes.append("old" if state == original else "new")

    assert swept_past_the_end, "the sweep never reached the end of a swap"
    assert "old" in outcomes
    assert "new" in outcomes, "a crash after the commit should finish the restore, not undo it"


def test_recovery_runs_before_bootstrap_so_the_admin_survives_a_crash(tmp_path: Path) -> None:
    """The original failure: config/ renamed away, then a restart, then a brand new admin
    with a brand new password, created over the account being restored."""
    harness = build_harness(tmp_path)
    active = harness.activate()
    data = harness.settings.data_dir
    retired = ".config.retired-20260101000000-abc123"
    (data / "config").rename(data / retired)
    maintenance_module.write_journal(
        data, retired={"config": retired}, state=maintenance_module.STATE_ACTIVATING
    )

    from app.main import create_app

    app = create_app(harness.settings)

    assert app.state.users.verify_password(active) is True
    assert not (data / JOURNAL_FILENAME).exists()


def test_a_journal_that_cannot_be_honoured_stops_startup_with_instructions(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    data = harness.settings.data_dir
    (data / JOURNAL_FILENAME).write_text("{", encoding="utf-8")

    from app.main import create_app

    with pytest.raises(RestoreRecoveryError) as caught:
        create_app(harness.settings)

    assert JOURNAL_FILENAME in str(caught.value)
    assert ".retired-" in str(caught.value)


def test_startup_clears_staging_a_kill_left_behind(tmp_path: Path) -> None:
    settings, _ = _make_settings(tmp_path, "data")
    for leftover in (".verify-staging-1", ".restore-staging-2"):
        (settings.data_dir / leftover / "config").mkdir(parents=True)

    recover_interrupted_restore(settings.data_dir)

    assert list(settings.data_dir.glob(".*-staging-*")) == []


def test_a_retired_directory_with_no_journal_is_left_for_its_owner(tmp_path: Path) -> None:
    """An earlier version's failed restore left originals like this. They may be all that
    remains of that data, and nothing here can tell."""
    settings, _ = _make_settings(tmp_path, "data")
    kept = settings.data_dir / ".config.retired-20260101000000"
    kept.mkdir()

    recover_interrupted_restore(settings.data_dir)

    assert kept.exists()


# --- a swap that fails while the process lives ---


def _failing_rename(monkeypatch: pytest.MonkeyPatch, failing_call: int) -> None:
    calls = [0]
    real_rename = Path.rename

    def rename(self: Path, target: object) -> object:
        calls[0] += 1
        if calls[0] == failing_call:
            raise OSError(28, "No space left on device")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


@pytest.mark.parametrize("failing_call", range(1, 9))
def test_a_rename_failing_anywhere_puts_every_original_back(
    prepared: tuple[Settings, BackupService, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    failing_call: int,
) -> None:
    settings, service, name, _, original = prepared
    _failing_rename(monkeypatch, failing_call)

    with pytest.raises(BackupError) as caught:
        service.restore_internal(name)

    assert "swapping data into place" in str(caught.value)
    _assert_whole(settings.data_dir, original)
    assert not filestore.write_lock().locked()


def test_a_commit_that_cannot_be_written_is_rolled_back(
    prepared: tuple[Settings, BackupService, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, service, name, _, original = prepared
    real_write = maintenance_module.write_journal

    def fail_on_commit(data_dir: Path, *, retired: dict[str, str], state: str) -> None:
        if state == maintenance_module.STATE_COMMITTED:
            raise OSError(28, "No space left on device")
        real_write(data_dir, retired=retired, state=state)

    monkeypatch.setattr(maintenance_module, "write_journal", fail_on_commit)

    with pytest.raises(BackupError):
        service.restore_internal(name)

    _assert_whole(settings.data_dir, original)


def test_a_failed_rollback_refuses_to_serve_and_a_restart_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, key = _make_settings(tmp_path, "data")
    _seed_report(settings)
    _write_markers(settings.data_dir, NEW_MARKER)
    gate = MaintenanceGate()
    service = _service(settings, key, gate)
    name = service.create()
    _write_markers(settings.data_dir, OLD_MARKER)
    original = _tree_hash(settings.data_dir)
    real_rename = Path.rename

    def rollback_also_fails(self: Path, target: object) -> object:
        if self.name == "history" or ".retired-" in self.name:
            raise OSError(5, "Input/output error")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rollback_also_fails)
    with pytest.raises(BackupRollbackError) as caught:
        service.restore_internal(name)

    assert ".retired-" in str(caught.value)
    assert gate.refusal is not None
    assert gate.try_enter() is False, "half-restored data must not be served"
    assert (settings.data_dir / JOURNAL_FILENAME).exists(), "the next start needs this"

    monkeypatch.setattr(Path, "rename", real_rename)
    recover_interrupted_restore(settings.data_dir)
    _assert_whole(settings.data_dir, original)


def test_a_subdir_the_archive_lacks_becomes_empty_not_mixed(
    tmp_path: Path,
) -> None:
    """Keeping it would put one install's history under another's settings."""
    source, key = _make_settings(tmp_path, "source")
    name = _service(source, key).create()
    blob = (source.backups_dir / name).read_bytes()
    target, _ = _make_settings(tmp_path, "target")
    _seed_report(target)

    _service(target, key).restore_external(blob)

    assert list(target.history_dir.glob("report-*")) == []


def test_a_restored_tree_still_accepts_new_files_in_emptied_subdirs(tmp_path: Path) -> None:
    source, key = _make_settings(tmp_path, "source")
    service = _service(source, key)
    name = service.create()
    service.restore_internal(name)

    filestore.atomic_write_bytes(source.cache_dir / "posters" / "after.jpg", b"jpeg")

    assert (source.cache_dir / "posters" / "after.jpg").exists()


# --- the gate ---


def test_exclusive_waits_for_work_in_flight_and_blocks_new_work() -> None:
    gate = MaintenanceGate()
    assert gate.try_enter()
    events: list[str] = []

    def finish_later() -> None:
        time.sleep(0.1)
        events.append("work-finished")
        gate.leave()

    thread = threading.Thread(target=finish_later)
    thread.start()
    with gate.exclusive():
        events.append("exclusive")
        assert gate.try_enter() is False
    thread.join(WAIT_SECONDS)

    assert events == ["work-finished", "exclusive"]
    assert gate.try_enter() is True, "the door reopens afterwards"


def test_exclusive_gives_up_truthfully_and_reopens() -> None:
    gate = MaintenanceGate()
    assert gate.try_enter()

    with pytest.raises(MaintenanceBusyError), gate.exclusive(timeout=0.01):
        pytest.fail("entered with work still running")

    gate.leave()
    assert gate.try_enter() is True


def test_two_maintenances_do_not_nest() -> None:
    gate = MaintenanceGate()
    with gate.exclusive(), pytest.raises(MaintenanceBusyError), gate.exclusive():
        pytest.fail("a second maintenance entered")


def test_exclusive_does_not_wait_for_its_own_caller() -> None:
    gate = MaintenanceGate()
    assert gate.try_enter()
    with gate.exclusive(callers_in_flight=1, timeout=0.01):
        pass
    gate.leave()


def test_exclusive_cancels_what_would_outlast_it() -> None:
    gate = MaintenanceGate()
    stopped: list[bool] = []
    gate.add_stop_hook(lambda: stopped.append(True))
    with gate.exclusive():
        assert stopped == [True]


async def test_background_work_holds_the_gate_until_it_has_actually_stopped() -> None:
    """A cancellation request is not completion. The count must outlast the task, even one
    cancelled before its first step, and cancelling from another thread must work."""
    gate = MaintenanceGate()
    background = SingleFlight(gate)
    gate.add_stop_hook(background.cancel_all)
    started = asyncio.Event()

    async def long_work() -> None:
        started.set()
        await asyncio.sleep(3600)

    assert background.start("long", long_work)
    await started.wait()

    def enter_exclusive() -> None:
        with gate.exclusive(timeout=WAIT_SECONDS):
            pass

    await asyncio.to_thread(enter_exclusive)

    await background.idle()
    assert gate.try_enter() is True


async def test_nothing_starts_while_the_door_is_closed() -> None:
    gate = MaintenanceGate()
    background = SingleFlight(gate)
    ran: list[bool] = []

    async def work() -> None:
        ran.append(True)

    with gate.exclusive():
        assert background.start("posters", work) is False
    await asyncio.sleep(0)
    assert ran == []


async def test_a_scheduled_job_is_skipped_while_the_door_is_closed() -> None:
    from app.services.scheduler import BoxMediaScheduler

    gate = MaintenanceGate()

    class _Pipeline:
        runs = 0

        async def run(self, **_: object) -> None:
            _Pipeline.runs += 1
            raise AssertionError("ran during a restore")

    scheduler = BoxMediaScheduler(_Pipeline(), interval_hours=24, maintenance=gate)  # type: ignore[arg-type]

    with gate.exclusive():
        await scheduler._run_scheduled()

    assert _Pipeline.runs == 0


def test_a_scheduled_job_holds_the_gate_while_it_runs() -> None:
    from app.services.scheduler import BoxMediaScheduler

    gate = MaintenanceGate()
    seen: list[bool] = []

    class _Pipeline:
        async def run(self, **_: object) -> object:
            # A restore arriving now has to wait: it cannot enter with this still running.
            def try_exclusive() -> None:
                with pytest.raises(MaintenanceBusyError), gate.exclusive(timeout=0.01):
                    pass
                seen.append(True)

            await asyncio.to_thread(try_exclusive)
            raise RuntimeError("stop here")

    scheduler = BoxMediaScheduler(_Pipeline(), interval_hours=24, maintenance=gate)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        asyncio.run(scheduler._run_scheduled())

    assert seen == [True]
    assert gate.try_enter() is True, "and released it, even though it failed"


def test_work_in_flight_lands_before_the_swap_and_cannot_overwrite_it(tmp_path: Path) -> None:
    """The stale-write race itself. A job that read before a restore and writes after it
    would put old data into the restored tree. Counted in, it makes the restore wait, so its
    write lands in the tree being replaced."""
    settings, key = _make_settings(tmp_path, "data")
    gate = MaintenanceGate()
    service = _service(settings, key, gate)
    name = service.create()
    assert gate.try_enter()
    stale = settings.history_dir / "written-by-the-job.json"
    swap_began = threading.Event()
    real_swap = service._swap_in

    def recording_swap(staging: Path) -> None:
        swap_began.set()
        real_swap(staging)

    service._swap_in = recording_swap  # type: ignore[method-assign]
    restore = threading.Thread(target=service.restore_internal, args=(name,))
    restore.start()

    assert not swap_began.wait(0.2), "the swap began with work still in flight"
    filestore.write_json(stale, {"from": "before the restore"}, schema_version=1)
    gate.leave()
    restore.join(WAIT_SECONDS)

    assert swap_began.is_set()
    assert not stale.exists()


def test_audit_appends_cannot_slip_between_the_renames(tmp_path: Path) -> None:
    """An append ignoring the swap would recreate logs/ between two renames and fail it."""
    audit = AuditLog(tmp_path / "logs" / "audit.jsonl")
    finished = threading.Event()

    def append() -> None:
        audit.record("anything")
        finished.set()

    with filestore.write_lock():
        thread = threading.Thread(target=append)
        thread.start()
        assert not finished.wait(0.1), "the append did not wait for the lock"
    thread.join(WAIT_SECONDS)

    assert finished.is_set()


def test_a_write_reaches_its_directory_entry_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    filestore.atomic_write_bytes(tmp_path / "file.txt", b"x")

    assert len(synced) == 2, "the file, then the directory holding its new name"


def test_a_filesystem_that_cannot_sync_directories_does_not_fail_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    calls = [0]

    def refuse_directories(fd: int) -> None:
        calls[0] += 1
        if calls[0] == 2:
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_directories)

    filestore.atomic_write_bytes(tmp_path / "file.txt", b"x")

    assert (tmp_path / "file.txt").read_bytes() == b"x"


# --- over HTTP ---


def _restore(harness: AppHarness, name: str):  # noqa: ANN202
    return harness.client.post(f"/settings/backups/{name}/restore", follow_redirects=False)


def test_a_restore_signs_everybody_out_and_says_so_at_login(harness: AppHarness) -> None:
    """Old sessions stayed authorized across a restored password and account."""
    active = harness.activate()
    name = harness.client.app.state.backups.create()
    other = CsrfClient(harness.client.app)
    other.post("/login", data={"username": "admin", "password": active}, follow_redirects=False)
    assert other.get("/library", follow_redirects=False).status_code == 200

    response = _restore(harness, name)

    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    assert SettingsStatus.BACKUP_RESTORED in response.headers["location"]
    assert other.get("/library", follow_redirects=False).headers["location"].endswith("/login")
    page = harness.client.get(response.headers["location"]).text
    assert "Sign in with the password from that backup" in page
    relogin = harness.client.post(
        "/login", data={"username": "admin", "password": active}, follow_redirects=False
    )
    assert relogin.status_code == 303
    assert harness.client.get("/library", follow_redirects=False).status_code == 200


def test_an_import_signs_everybody_out_too(harness: AppHarness) -> None:
    harness.activate()
    service = harness.client.app.state.backups
    name = service.create()

    response = harness.client.post(
        "/settings/backups/import",
        files={"backup_file": (name, service.path_for(name).read_bytes())},
        follow_redirects=False,
    )

    assert SettingsStatus.BACKUP_IMPORTED in response.headers["location"]
    assert harness.client.get("/library", follow_redirects=False).status_code == 303


def test_the_login_page_prints_only_its_own_notices(harness: AppHarness) -> None:
    page = harness.client.get("/login?status=<script>alert(1)</script>").text
    assert "alert(1)" not in page


def test_in_memory_state_from_before_a_restore_is_forgotten(harness: AppHarness) -> None:
    harness.activate()
    state = harness.client.app.state
    name = state.backups.create()
    state.radarr_library_snapshots.put("app-old", {})
    state.radarr_backoff.note_failure("app-old")
    state.media_server_backoff.note_failure("app-old")
    state.poster_prefetcher._pending.add("https://image.tmdb.org/t/p/w342/old.jpg")

    _restore(harness, name)

    assert state.radarr_library_snapshots.get("app-old") is None
    assert not state.radarr_backoff.should_skip("app-old")
    assert not state.media_server_backoff.should_skip("app-old")
    assert state.poster_prefetcher._pending == set()


def test_a_failed_restore_keeps_everybody_signed_in(harness: AppHarness) -> None:
    harness.activate()
    state = harness.client.app.state
    name = state.backups.create()

    def fail(_: Path) -> None:
        raise OSError(28, "No space left on device")

    state.backups._swap_in = fail

    response = _restore(harness, name)

    assert SettingsStatus.BACKUP_FAILED in response.headers["location"]
    assert harness.client.get("/library", follow_redirects=False).status_code == 200


def test_work_that_will_not_finish_gets_a_truthful_busy_answer(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.activate()
    state = harness.client.app.state
    name = state.backups.create()
    monkeypatch.setattr(
        state.maintenance, "exclusive",
        lambda **_: (_ for _ in ()).throw(MaintenanceBusyError("still running")),
    )
    before = _tree_hash(harness.settings.data_dir)

    response = _restore(harness, name)

    assert SettingsStatus.BACKUP_BUSY in response.headers["location"]
    assert _tree_hash(harness.settings.data_dir) == before
    assert "Nothing was changed" in harness.client.get(response.headers["location"]).text


def test_busy_is_a_backup_error_the_scheduler_and_routes_already_handle() -> None:
    assert issubclass(BackupBusyError, BackupError)
    assert issubclass(BackupRollbackError, BackupError)


def test_requests_get_a_clear_503_while_the_door_is_closed(harness: AppHarness) -> None:
    harness.activate()
    gate = harness.client.app.state.maintenance

    with gate.exclusive():
        page = harness.client.get("/library", follow_redirects=False)
        health = harness.client.get("/health")

    assert page.status_code == 503
    assert "restoring" in page.text
    assert page.headers["retry-after"]
    assert health.status_code == 200, "a monitor must not restart a restore"
    assert harness.client.get("/library", follow_redirects=False).status_code == 200


def test_a_refused_process_says_what_needs_doing_on_every_page(harness: AppHarness) -> None:
    harness.activate()
    harness.client.app.state.maintenance.refuse("rename .config.retired-1 back to config")

    page = harness.client.get("/library", follow_redirects=False)

    assert page.status_code == 503
    assert ".config.retired-1" in page.text
