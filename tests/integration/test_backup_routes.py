"""Steps 18–20 HTTP test: backup create/restore round-trip through the app."""

from __future__ import annotations

import pytest

from app.web.settings import SettingsStatus
from tests.conftest import AppHarness

NEW_PASSWORD = "second9password"


def test_backup_create_download_restore_roundtrip(harness: AppHarness) -> None:
    active = harness.activate()

    # Create a backup of the current (activated) state.
    created = harness.client.post("/settings/backups/create", follow_redirects=False)
    assert SettingsStatus.BACKUP_CREATED in created.headers["location"]
    backups = harness.client.app.state.backups.list_backups()
    assert len(backups) == 1
    name = backups[0].name

    # The encrypted archive downloads.
    download = harness.client.get(f"/settings/backups/{name}/download")
    assert download.status_code == 200
    assert len(download.content) > 0

    # Change the password, then restore — the earlier password must work again.
    harness.client.post(
        "/account/password",
        data={"current_password": active, "new_password": NEW_PASSWORD,
              "confirm_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert harness.users.verify_password(NEW_PASSWORD) is True

    restored = harness.client.post(f"/settings/backups/{name}/restore", follow_redirects=False)
    assert SettingsStatus.BACKUP_RESTORED in restored.headers["location"]
    assert harness.users.verify_password(active) is True  # back to the backed-up state


def test_import_rejects_garbage_file(harness: AppHarness) -> None:
    """Bytes that are not an archive fail the AES-GCM tag check, which is the same event
    as a wrong key — so this is the "could not be unlocked" banner, which names both, and
    no longer the catch-all sentence every failure used to share."""
    harness.activate()
    response = harness.client.post(
        "/settings/backups/import",
        files={"backup_file": ("evil.backup", b"not a real backup", "application/octet-stream")},
        follow_redirects=False,
    )
    assert SettingsStatus.BACKUP_BAD_KEY in response.headers["location"]


def test_import_rejects_oversized_file(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch the cap tiny so we exercise the bounded read + rejection without pushing
    # 100 MB through the suite. The route reads MAX_UPLOAD_BYTES+1 bytes; restore_external
    # then rejects len(blob) > MAX_UPLOAD_BYTES.
    monkeypatch.setattr("app.web.settings.MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr("app.services.backup.MAX_UPLOAD_BYTES", 8)
    harness.activate()
    response = harness.client.post(
        "/settings/backups/import",
        files={"backup_file": ("big.backup", b"x" * 64, "application/octet-stream")},
        follow_redirects=False,
    )
    assert SettingsStatus.BACKUP_FAILED in response.headers["location"]


def test_backups_table_shows_creation_date_and_readable_size(harness: AppHarness) -> None:
    harness.activate()
    harness.client.post("/settings/backups/create", follow_redirects=False)
    page = harness.client.get("/settings").text

    assert "<th>Created</th>" in page
    assert "· latest" in page  # the newest row is called out
    created = harness.client.app.state.backups.list_backups()[0].created_at
    assert f"{created.day}/{created.month}/{created.year}" in page  # day-first
    assert "KB" in page or "MB" in page


CONFIGURED_KEEP = 15
BACKUPS_TO_CREATE = 12


def _set_backup_keep(harness: AppHarness, keep: int) -> None:
    harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "0", "backup_keep": str(keep)},
        follow_redirects=False,
    )


def test_manual_create_honours_the_configured_retention(harness: AppHarness) -> None:
    """Create Backup must prune to the admin's "Keep the last (backups)", not to 10.

    Regression: the route called create() with no `keep`, so the service pruned to its
    own DEFAULT_KEEP = 10 and silently deleted archives 11..N for anyone who raised the
    setting.
    """
    harness.activate()
    _set_backup_keep(harness, CONFIGURED_KEEP)
    assert harness.client.app.state.filters.load().backup_keep == CONFIGURED_KEEP

    for _ in range(BACKUPS_TO_CREATE):
        harness.client.post("/settings/backups/create", follow_redirects=False)

    assert len(harness.client.app.state.backups.list_backups()) == BACKUPS_TO_CREATE


def test_manual_create_still_prunes_at_the_configured_limit(harness: AppHarness) -> None:
    # The retention is honoured, not ignored: two past the limit still leaves the limit.
    harness.activate()
    _set_backup_keep(harness, 3)

    for _ in range(5):
        harness.client.post("/settings/backups/create", follow_redirects=False)

    assert len(harness.client.app.state.backups.list_backups()) == 3


def test_restore_safety_backup_never_prunes(harness: AppHarness) -> None:
    """The pre-restore snapshot is a safety net — it must not delete other archives.

    Deleting backups part-way through a restore is the worst possible moment for it.
    """
    harness.activate()
    _set_backup_keep(harness, CONFIGURED_KEEP)
    for _ in range(3):
        harness.client.post("/settings/backups/create", follow_redirects=False)
    before = harness.client.app.state.backups.list_backups()
    target = before[-1].name  # restore the oldest

    restored = harness.client.post(
        f"/settings/backups/{target}/restore", follow_redirects=False
    )
    assert SettingsStatus.BACKUP_RESTORED in restored.headers["location"]

    after = harness.client.app.state.backups.list_backups()
    assert len(after) == len(before) + 1  # the safety net was added, nothing removed
    assert {info.name for info in before} <= {info.name for info in after}


def test_a_failed_swap_answers_with_the_banner_not_a_500(harness: AppHarness) -> None:
    """End of the chain: BackupError is what the route catches, so mapping the swap's
    OSError to it is what turns a 500 on a half-swapped tree into "Nothing was changed"."""
    harness.activate()
    name = harness.client.app.state.backups.create()
    harness.client.app.state.backups._swap_in = _raise_directory_not_empty

    response = harness.client.post(
        f"/settings/backups/{name}/restore", follow_redirects=False
    )

    assert response.status_code == 303
    assert SettingsStatus.BACKUP_FAILED in response.headers["location"]


def _raise_directory_not_empty(staging: object) -> None:
    raise OSError(39, "Directory not empty")


# --- verify: would this restore? (F17) ---


def _stores_hash(root) -> str:  # noqa: ANN001
    """A digest of every stored file under /data — path, size and bytes.

    The audit log is excluded, and only it: a verify records that it ran, exactly as every
    other action does, and suppressing that to make an assertion pass would be trading a
    real guarantee for a neater test. Everything the archive would REPLACE is in here, so
    a verify that touched a store, or left a staging directory behind, changes this digest
    even when nothing visible broke.
    """
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == "audit.jsonl":
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(str(path.stat().st_size).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _rewrite_archive(
    harness: AppHarness, name: str, *, drop_a_listed_file: bool = False,
    corrupt_a_listed_file: bool = False, bump_schema: bool = False,
) -> None:
    """Rebuild an archive's contents and re-encrypt with the install's OWN key.

    That last part is what makes these tests test anything: an archive that fails the tag
    check never reaches the checksum or schema passes, so a corruption test that simply
    flipped a byte would be indistinguishable from the wrong-key one. This produces an
    archive that decrypts cleanly and then fails further in.
    """
    import io
    import json
    import tarfile

    from app.core import crypto
    from app.services.backup import MANIFEST_NAME

    service = harness.client.app.state.backups
    path = service.path_for(name)
    plaintext = crypto.decrypt_bytes(path.read_bytes(), service._key)

    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            members[member.name] = extracted.read() if extracted else b""

    if drop_a_listed_file:
        listed = json.loads(members[MANIFEST_NAME])["files"]
        members.pop(next(iter(listed)))
    if corrupt_a_listed_file:
        listed = json.loads(members[MANIFEST_NAME])["files"]
        target = next(iter(listed))
        members[target] = members[target] + b"\n# appended after the manifest was written"
    if bump_schema:
        target = next(n for n in members if n.endswith("user.yml"))
        members[target] = members[target].replace(b"schema_version: 1", b"schema_version: 99")
        listed = json.loads(members[MANIFEST_NAME])
        import hashlib
        listed["files"][target] = hashlib.sha256(members[target]).hexdigest()
        members[MANIFEST_NAME] = json.dumps(listed).encode()

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for arcname, data in members.items():
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(crypto.encrypt_bytes(buffer.getvalue(), service._key))


def _one_backup(harness: AppHarness) -> str:
    harness.client.post("/settings/backups/create", follow_redirects=False)
    return harness.client.app.state.backups.list_backups()[0].name


def test_a_good_archive_verifies_and_changes_nothing(harness: AppHarness) -> None:
    """Until now the only way to learn an archive was readable was to restore it and find
    out — a poor moment to discover a wrong key."""
    harness.activate()
    name = _one_backup(harness)
    data_dir = harness.client.app.state.settings.data_dir
    before = _stores_hash(data_dir)
    audit_before = harness.audit_lines()

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_VERIFIED in response.headers["location"]
    assert _stores_hash(data_dir) == before, "verify must leave every store untouched"
    assert not list(data_dir.glob(".verify-staging-*")), "staging was left behind"
    # The one thing it is allowed to add, and only ever by appending.
    assert audit_before == harness.audit_lines()[: len(audit_before)]


def test_an_archive_from_another_key_says_it_cannot_be_unlocked(
    harness: AppHarness,
) -> None:
    """The banner names the key AND the file, because AES-GCM cannot tell them apart: a
    failed tag check is the same event either way."""
    from app.core import crypto

    harness.activate()
    name = _one_backup(harness)
    path = harness.client.app.state.backups.path_for(name)
    # Re-encrypt the same plaintext under a key this install does not have.
    plaintext = crypto.decrypt_bytes(path.read_bytes(), harness.client.app.state.backups._key)
    path.write_bytes(crypto.encrypt_bytes(plaintext, crypto.generate_key()))

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_BAD_KEY in response.headers["location"]


def test_a_tampered_archive_says_it_cannot_be_unlocked(harness: AppHarness) -> None:
    """One flipped byte fails the same authentication — which is the point of encrypting
    with an AEAD rather than checksumming afterwards."""
    harness.activate()
    name = _one_backup(harness)
    path = harness.client.app.state.backups.path_for(name)
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    path.write_bytes(bytes(blob))

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_BAD_KEY in response.headers["location"]


def test_an_archive_missing_a_listed_file_reports_corruption(harness: AppHarness) -> None:
    """Past the tag check, so it decrypted: the manifest lists a file the archive does not
    carry. A different answer from a wrong key, and a different thing to do about it."""
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, drop_a_listed_file=True)

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_CORRUPT in response.headers["location"]


def test_an_archive_whose_bytes_changed_reports_corruption(harness: AppHarness) -> None:
    """The other half of the corrupt branch: the file is there, and it is not what the
    manifest says it is. Distinct from a missing file, and from a failed tag check."""
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, corrupt_a_listed_file=True)

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_CORRUPT in response.headers["location"]


def test_an_archive_from_a_newer_build_says_to_upgrade(harness: AppHarness) -> None:
    """Intact bytes this build does not understand. Restoring it would succeed and then
    raise on every page, so the question is asked while backing out is still free."""
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, bump_schema=True)

    response = harness.client.post(
        f"/settings/backups/{name}/verify", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_NEWER_SCHEMA in response.headers["location"]


def test_a_failed_verify_still_leaves_data_untouched(harness: AppHarness) -> None:
    """The staging directory goes in `finally`, so a failure cleans up as thoroughly as a
    success — and the pre-restore safety archive is never taken, because nothing is being
    replaced."""
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, bump_schema=True)
    data_dir = harness.client.app.state.settings.data_dir
    before = _stores_hash(data_dir)

    harness.client.post(f"/settings/backups/{name}/verify", follow_redirects=False)

    assert _stores_hash(data_dir) == before
    assert not list(data_dir.glob(".verify-staging-*"))
    assert len(harness.client.app.state.backups.list_backups()) == 1  # no safety archive


def test_verify_is_recorded_and_a_failure_says_why_in_the_audit(
    harness: AppHarness,
) -> None:
    """The banner is a closed enum; the specific reason still has to be findable."""
    harness.activate()
    name = _one_backup(harness)
    harness.client.post(f"/settings/backups/{name}/verify", follow_redirects=False)
    _rewrite_archive(harness, name, bump_schema=True)
    harness.client.post(f"/settings/backups/{name}/verify", follow_redirects=False)

    lines = "\n".join(harness.audit_lines())

    assert "backup_verified" in lines
    assert "backup_failed" in lines
    assert "unreadable" in lines  # the exception's own words, in the log rather than the UI


def test_a_restore_explains_itself_with_the_same_banners(harness: AppHarness) -> None:
    """Verify is not the only place the reason matters. A restore that refuses an archive
    from a newer build used to say only "the backup operation failed" — the same sentence
    a wrong key got, and the same one a truncated file got.
    """
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, bump_schema=True)
    data_dir = harness.client.app.state.settings.data_dir
    before = _stores_hash(data_dir)

    response = harness.client.post(
        f"/settings/backups/{name}/restore", follow_redirects=False
    )

    assert SettingsStatus.BACKUP_NEWER_SCHEMA in response.headers["location"]
    # ...and it refused before touching anything, which is the whole point of asking the
    # schema question ahead of the swap.
    assert _stores_hash(data_dir) == before


def test_an_import_explains_itself_with_the_same_banners(harness: AppHarness) -> None:
    harness.activate()
    name = _one_backup(harness)
    _rewrite_archive(harness, name, corrupt_a_listed_file=True)
    blob = harness.client.app.state.backups.path_for(name).read_bytes()

    response = harness.client.post(
        "/settings/backups/import",
        files={"backup_file": ("x.backup", blob, "application/octet-stream")},
        follow_redirects=False,
    )

    assert SettingsStatus.BACKUP_CORRUPT in response.headers["location"]


def test_the_backup_row_offers_verify_without_a_confirm_step(harness: AppHarness) -> None:
    """Restore and Delete sit behind a confirm because they are irreversible. This one
    reads into a staging directory it deletes again, so asking "are you sure?" here is how
    a confirm stops meaning anything on the two buttons that need one."""
    harness.activate()
    name = _one_backup(harness)

    page = harness.client.get("/settings").text
    row = page.split(name, 1)[1].split("</tr>", 1)[0]

    assert f"/settings/backups/{name}/verify" in row
    assert ">Verify<" in row
    # A POST with the session's token, like every other mutating control in the app.
    verify_form = row.split("/verify", 1)[1].split("</form>", 1)[0]
    assert 'name="csrf_token"' in verify_form
    # ...and not wrapped in a <details> confirm, unlike the two beside it.
    assert "<details" not in row.split("/verify", 1)[0].rsplit("<form", 1)[1]
