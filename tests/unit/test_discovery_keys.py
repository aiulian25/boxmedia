"""TV step 5 unit test: discovery credentials encrypted at rest, and the redactor.

The redactor gets as much attention as the store because it is what pays for the one
place this app puts a credential in a URL — TMDB's v3 API takes the key as a query
parameter, which is their contract, not our choice.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.core import crypto
from app.core.audit import AuditAction, AuditLog
from app.services.apps import API_KEY_MASK
from app.services.discovery import (
    KEY_MASK,
    PROVIDER_TMDB,
    PROVIDER_TRAKT,
    REDACTED,
    TMDB_BASE_URL,
    TRAKT_BASE_URL,
    DiscoveryError,
    DiscoveryStore,
    ProbeResult,
    probe_tmdb,
    probe_trakt,
    redact,
)

# Shaped like the real ones, and deliberately NOT the real ones — see the last test.
TMDB_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TRAKT_ID = "Zx-9QwErTyUiOpAsDfGhJkLzXcVbNm1234567890abc"


@pytest.fixture
def store(tmp_path: Path) -> DiscoveryStore:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return DiscoveryStore(tmp_path, key=crypto.generate_key(), audit=audit)


# --- storage ---


def test_keys_are_encrypted_at_rest(store: DiscoveryStore, tmp_path: Path) -> None:
    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.save(PROVIDER_TRAKT, TRAKT_ID)

    raw = (tmp_path / "discovery.yml").read_text(encoding="utf-8")
    assert TMDB_KEY not in raw
    assert TRAKT_ID not in raw
    assert raw.count("gcm:v1:") == 2
    # And they round-trip for the clients that must send them.
    assert store.tmdb_key() == TMDB_KEY
    assert store.trakt_client_id() == TRAKT_ID


def test_load_says_what_exists_and_never_what_it_is(store: DiscoveryStore) -> None:
    """This is what the Settings page renders. A template that cannot reach a secret
    cannot leak one."""
    assert store.load().public() == {
        "has_tmdb": False, "has_trakt": False, "ready": False, "mask": KEY_MASK,
    }

    store.save(PROVIDER_TMDB, TMDB_KEY)
    view = store.load().public()

    assert view["has_tmdb"] is True
    assert view["ready"] is False  # Discover needs both
    assert TMDB_KEY not in str(view)


def test_both_present_is_ready(store: DiscoveryStore) -> None:
    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.save(PROVIDER_TRAKT, TRAKT_ID)
    assert store.load().ready is True


def test_a_blank_value_keeps_the_stored_one(store: DiscoveryStore) -> None:
    """The contract every secret field in this app has: it is what lets someone re-save
    the card without re-pasting what is already there."""
    store.save(PROVIDER_TMDB, TMDB_KEY)

    store.save(PROVIDER_TMDB, "")
    store.save(PROVIDER_TMDB, "   ")

    assert store.tmdb_key() == TMDB_KEY


def test_the_mask_is_never_stored_as_a_key(store: DiscoveryStore) -> None:
    """A browser that filled the placeholder into the field, or a person who copied it,
    must not overwrite a real key with a row of dots."""
    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.save(PROVIDER_TMDB, KEY_MASK)
    assert store.tmdb_key() == TMDB_KEY


def test_saving_one_key_leaves_the_other_alone(store: DiscoveryStore) -> None:
    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.save(PROVIDER_TRAKT, TRAKT_ID)

    store.save(PROVIDER_TMDB, "a" * 32)

    assert store.trakt_client_id() == TRAKT_ID


def test_delete_is_its_own_gesture(store: DiscoveryStore) -> None:
    """Blank means keep; deleting is explicit. The two must not be one gesture."""
    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.save(PROVIDER_TRAKT, TRAKT_ID)

    assert store.remove(PROVIDER_TMDB) is True

    assert store.tmdb_key() is None
    assert store.trakt_client_id() == TRAKT_ID
    assert store.load().has_tmdb is False


def test_deleting_what_is_not_there_is_not_an_error(store: DiscoveryStore) -> None:
    assert store.remove(PROVIDER_TRAKT) is False


def test_an_unknown_provider_is_refused(store: DiscoveryStore) -> None:
    for call in (
        lambda: store.save("lastfm", "x"),
        lambda: store.remove("lastfm"),
        lambda: store.decrypt("lastfm"),
    ):
        with pytest.raises(DiscoveryError):
            call()


def test_the_audit_records_the_action_not_the_value(
    store: DiscoveryStore, tmp_path: Path
) -> None:
    """A credential must not reach the audit log — nor its length, which narrows a guess.

    The records are PARSED rather than substring-searched. Looking for `str(len(key))` in
    the raw text was flaky at about one run in seven: the length is 32, and so is every
    timestamp that happens to land on the 32nd second.
    """
    import json

    store.save(PROVIDER_TMDB, TMDB_KEY)
    store.remove(PROVIDER_TMDB)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    actions = [record.get("action") for record in records]
    assert AuditAction.DISCOVERY_KEY_SAVED in actions
    assert AuditAction.DISCOVERY_KEY_REMOVED in actions

    for record in records:
        assert record.get("provider") in (None, PROVIDER_TMDB)
        # Nothing anywhere in the row is the secret, or measures it.
        for value in record.values():
            assert value != TMDB_KEY
            assert not (isinstance(value, str) and TMDB_KEY in value)
            assert value != len(TMDB_KEY)


def test_the_mask_matches_the_connection_cards(store: DiscoveryStore) -> None:
    """Mirrored rather than imported so a credential store does not depend on the
    connection store for a UI string. Pinned so the two cannot drift."""
    assert KEY_MASK == API_KEY_MASK


def test_a_missing_file_reads_as_nothing_configured(tmp_path: Path) -> None:
    store = DiscoveryStore(tmp_path, key=crypto.generate_key())
    assert store.load().has_tmdb is False
    assert store.tmdb_key() is None


# --- the redactor: what pays for the query-parameter deviation ---


def test_redact_strips_the_key_out_of_a_url() -> None:
    text = f"cannot reach {TMDB_BASE_URL}/tv/1396?api_key={TMDB_KEY}&language=en"
    cleaned = redact(text)

    assert TMDB_KEY not in cleaned
    assert f"api_key={REDACTED}" in cleaned
    # Everything else survives, or the message stops being diagnosable.
    assert "language=en" in cleaned
    assert "/tv/1396" in cleaned


def test_redact_is_value_blind() -> None:
    """A redactor that only knew the CURRENT key would leak a rotated one — and would
    leak a key typed into the Test box before it was ever stored."""
    # Asserted by equality rather than absence: `"" in anything` is True, so an
    # absence check quietly passes for the empty case and proves nothing.
    for value in ("anything-at-all", "", "a" * 200, "%20weird%20", "-_.~"):
        assert redact(f"?api_key={value}&x=1") == f"?api_key={REDACTED}&x=1"


def test_redact_handles_the_shapes_an_exception_actually_carries() -> None:
    cases = [
        f"api_key={TMDB_KEY}",  # end of string
        f"api_key={TMDB_KEY}&page=2",  # followed by another param
        f"'{TMDB_BASE_URL}/x?api_key={TMDB_KEY}'",  # quoted, as httpx repr does
        f"<Request('GET', '{TMDB_BASE_URL}/x?api_key={TMDB_KEY}')>",
        f"API_KEY={TMDB_KEY}",  # case-insensitive
    ]
    for case in cases:
        assert TMDB_KEY not in redact(case), case


def test_redact_accepts_a_non_string() -> None:
    """It is applied to exception objects, not just their text."""
    assert TMDB_KEY not in redact(ValueError(f"boom ?api_key={TMDB_KEY}"))


# --- probes ---


@respx.mock
async def test_a_good_tmdb_key_is_ok() -> None:
    respx.get(f"{TMDB_BASE_URL}/configuration").mock(
        return_value=httpx.Response(200, json={"images": {}})
    )
    assert await probe_tmdb(TMDB_KEY) == ProbeResult.OK


@respx.mock
async def test_a_rejected_tmdb_key_is_auth() -> None:
    respx.get(f"{TMDB_BASE_URL}/configuration").mock(return_value=httpx.Response(401))
    assert await probe_tmdb(TMDB_KEY) == ProbeResult.AUTH


@respx.mock
async def test_an_unreachable_tmdb_is_unreachable_not_an_exception() -> None:
    """A probe reports; it does not fail. The dropped exception's text carries the
    request URL, and for TMDB that URL carries the key."""
    respx.get(f"{TMDB_BASE_URL}/configuration").mock(
        side_effect=httpx.ConnectError("no route")
    )
    assert await probe_tmdb(TMDB_KEY) == ProbeResult.UNREACHABLE


@respx.mock
async def test_the_tmdb_probe_sends_the_key_as_tmdb_requires() -> None:
    route = respx.get(f"{TMDB_BASE_URL}/configuration").mock(
        return_value=httpx.Response(200, json={})
    )
    await probe_tmdb(TMDB_KEY)

    request = route.calls.last.request
    # Their contract, not our choice — and precisely why `redact` exists.
    assert request.url.params["api_key"] == TMDB_KEY
    assert request.headers["User-Agent"].startswith("BoxMedia/")


@respx.mock
async def test_a_good_trakt_id_is_ok_and_travels_in_a_header() -> None:
    route = respx.get(f"{TRAKT_BASE_URL}/shows/trending").mock(
        return_value=httpx.Response(200, json=[{"show": {"title": "X"}}])
    )
    assert await probe_trakt(TRAKT_ID) == ProbeResult.OK

    request = route.calls.last.request
    assert request.headers["trakt-api-key"] == TRAKT_ID
    assert request.headers["trakt-api-version"] == "2"
    # A header, so it never reaches a proxy log or browser history.
    assert TRAKT_ID not in str(request.url)
    # One row is all a credential check needs.
    assert request.url.params["limit"] == "1"


@respx.mock
async def test_a_rejected_trakt_id_is_auth() -> None:
    respx.get(f"{TRAKT_BASE_URL}/shows/trending").mock(return_value=httpx.Response(403))
    assert await probe_trakt(TRAKT_ID) == ProbeResult.AUTH


@respx.mock
async def test_a_server_error_is_unreachable_rather_than_a_pass() -> None:
    respx.get(f"{TRAKT_BASE_URL}/shows/trending").mock(return_value=httpx.Response(500))
    assert await probe_trakt(TRAKT_ID) == ProbeResult.UNREACHABLE


# --- the keys from the teardown must not be anywhere in this repo ---


def test_no_real_credential_is_committed_anywhere() -> None:
    """tv-discovery.md carries a live TMDB key and Trakt client ID in its section 0. It
    is gitignored and is reference material, never a source file: the app ships no keys,
    and the dev instance gets these typed into Settings, where they land encrypted.

    This walks the tracked tree so a fixture, template or test that pasted one in fails
    here rather than in a push.
    """
    import subprocess

    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "ls-files", "-z"],  # noqa: S607 — git is on PATH in dev and CI
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.split("\0")

    # Split so this file does not itself contain either literal.
    needles = (
        "b48e7617" + "79825178094eb85ccc8f37d4",
        "BsShkZ_8s95pdstNgQ7Ij0YF89O0" + "nlqpiCd7BFXNY-M",
    )
    offenders = []
    for name in tracked:
        path = root / name
        if not name or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(needle in body for needle in needles):
            offenders.append(name)

    assert not offenders, f"a real discovery credential is committed in: {offenders}"
