"""Step 10 unit test: API keys encrypted at rest, CRUD, URL normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import crypto
from app.core.audit import AuditLog
from app.services.apps import (
    API_KEY_MASK,
    MAX_APP_NAME_LENGTH,
    AppNotFoundError,
    AppsStore,
    InvalidAppError,
    normalize_url,
)

RADARR_KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def store(tmp_path: Path) -> AppsStore:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return AppsStore(tmp_path, key=crypto.generate_key(), audit=audit)


def test_add_encrypts_key_at_rest(store: AppsStore, tmp_path: Path) -> None:
    app = store.add(name="Radarr - Main", url="192.168.1.100:7878", api_key=RADARR_KEY)
    raw = (tmp_path / "apps.yml").read_text(encoding="utf-8")
    # The plaintext key is never on disk; only its gcm token is.
    assert RADARR_KEY not in raw
    assert "gcm:v1:" in raw
    # Round-trips back to the original for the pipeline / test-connection.
    assert store.decrypt_key(app.id) == RADARR_KEY


def test_add_normalizes_url(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="192.168.1.100:7878", api_key=RADARR_KEY)
    assert app.url == "http://192.168.1.100:7878"


def test_update_blank_key_keeps_existing(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="radarr.local:7878", api_key=RADARR_KEY)
    store.update(app.id, name="Radarr 2", url="radarr.local:7878", api_key="")
    assert store.decrypt_key(app.id) == RADARR_KEY  # unchanged
    assert store.get(app.id).name == "Radarr 2"


def test_update_new_key_replaces(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="radarr.local:7878", api_key=RADARR_KEY)
    store.update(app.id, name="Radarr", url="radarr.local:7878", api_key="newkey123456")
    assert store.decrypt_key(app.id) == "newkey123456"


def test_update_ignores_mask_sentinel(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="radarr.local:7878", api_key=RADARR_KEY)
    store.update(app.id, name="Radarr", url="radarr.local:7878", api_key=API_KEY_MASK)
    assert store.decrypt_key(app.id) == RADARR_KEY


def test_remove(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="radarr.local:7878", api_key=RADARR_KEY)
    store.remove(app.id)
    assert store.list_apps() == []
    with pytest.raises(AppNotFoundError):
        store.get(app.id)


def test_public_view_masks_key(store: AppsStore) -> None:
    app = store.add(name="Radarr", url="radarr.local:7878", api_key=RADARR_KEY)
    public = app.public()
    assert public["api_key_mask"] == API_KEY_MASK
    assert "api_key_encrypted" not in public


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.100:7878", "http://192.168.1.100:7878"),
        ("https://radarr.example/", "https://radarr.example"),
        ("http://radarr.local:7878", "http://radarr.local:7878"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_rejects_empty() -> None:
    with pytest.raises(InvalidAppError):
        normalize_url("   ")


def test_rotated_keys_open_with_the_new_key_through_the_store(tmp_path: Path) -> None:
    """The acceptance path for `crypto rotate`: after rotating and swapping the key file,
    the app decrypts the same Radarr API key — which is exactly what Test Connection and
    every pipeline run rely on (`AppsStore.decrypt_key`)."""
    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    audit = AuditLog(tmp_path / "audit.jsonl")

    old_file, new_file = tmp_path / "old.key", tmp_path / "new.key"
    assert crypto._main(["genkey", str(old_file)]) == 0
    assert crypto._main(["genkey", str(new_file)]) == 0

    before = AppsStore(config_dir, key=crypto.load_key(old_file), audit=audit)
    app = before.add(name="Radarr", url="http://radarr.local:7878", api_key=RADARR_KEY)

    assert crypto._main(["rotate", str(old_file), str(new_file), str(data_dir)]) == 0

    after = AppsStore(config_dir, key=crypto.load_key(new_file), audit=audit)
    assert after.decrypt_key(app.id) == RADARR_KEY
    assert after.get(app.id).url == "http://radarr.local:7878"  # other fields intact

    stale = AppsStore(config_dir, key=crypto.load_key(old_file), audit=audit)
    with pytest.raises(crypto.DecryptionError):
        stale.decrypt_key(app.id)  # the old key no longer opens the store


# --- primary connection (F9) ---


def test_primary_defaults_to_the_first_connection(store: AppsStore) -> None:
    # Old apps.yml files carry no `primary` key: the first stays in charge, as before.
    first = store.add(name="A", url="a.local:7878", api_key=RADARR_KEY)
    store.add(name="B", url="b.local:7878", api_key=RADARR_KEY)
    assert store.primary_id() == first.id
    assert store.get(first.id).primary is False  # implicit, not yet flagged


def test_set_primary_is_exclusive(store: AppsStore) -> None:
    first = store.add(name="A", url="a.local:7878", api_key=RADARR_KEY)
    second = store.add(name="B", url="b.local:7878", api_key=RADARR_KEY)

    store.set_primary(second.id)
    assert store.primary_id() == second.id
    assert [app.primary for app in store.list_apps()] == [False, True]

    store.set_primary(first.id)  # flipping back clears the other
    assert [app.primary for app in store.list_apps()] == [True, False]


def test_set_primary_rejects_an_unknown_id(store: AppsStore) -> None:
    store.add(name="A", url="a.local:7878", api_key=RADARR_KEY)
    with pytest.raises(AppNotFoundError):
        store.set_primary("app-nope")


def test_removing_the_primary_promotes_another(store: AppsStore) -> None:
    first = store.add(name="A", url="a.local:7878", api_key=RADARR_KEY)
    second = store.add(name="B", url="b.local:7878", api_key=RADARR_KEY)
    store.set_primary(second.id)

    store.remove(second.id)
    assert store.primary_id() == first.id  # never points at a deleted connection
    assert store.get(first.id).primary is True


def test_primary_id_is_none_without_connections(store: AppsStore) -> None:
    assert store.primary_id() is None


def test_a_connection_name_is_bounded(store: AppsStore) -> None:
    """The name renders in the Add button, the target menu and the "In Library · X"
    badge — all inside a 208px poster card."""
    with pytest.raises(InvalidAppError):
        store.add(name="x" * (MAX_APP_NAME_LENGTH + 1), url="radarr.local", api_key=RADARR_KEY)


def test_a_name_at_the_limit_is_accepted(store: AppsStore) -> None:
    at_limit = "x" * MAX_APP_NAME_LENGTH
    app = store.add(name=at_limit, url="radarr.local", api_key=RADARR_KEY)
    assert app.name == at_limit


def test_renaming_is_bounded_too(store: AppsStore) -> None:
    # A rename flows straight to the Add menu, so it is checked on the same terms as add.
    app = store.add(name="Local", url="radarr.local", api_key=RADARR_KEY)
    with pytest.raises(InvalidAppError):
        store.update(
            app.id, name="y" * (MAX_APP_NAME_LENGTH + 1), url="radarr.local", api_key=None
        )
    assert store.get(app.id).name == "Local"  # unchanged


def test_a_name_is_trimmed_not_rejected_for_stray_spaces(store: AppsStore) -> None:
    app = store.add(name="  Pizza  ", url="radarr.local", api_key=RADARR_KEY)
    assert app.name == "Pizza"


def test_renaming_keeps_the_connection_usable(store: AppsStore) -> None:
    """A rename must not disturb the id, the key or the per-connection defaults — the
    menu reads the live name, everything else keys off the id."""
    app = store.add(name="Local", url="radarr.local", api_key=RADARR_KEY)
    store.set_defaults(app.id, quality_profile_id=4, root_folder="/movies")

    store.update(app.id, name="Pizza", url="radarr.local", api_key=None)

    renamed = store.get(app.id)
    assert renamed.id == app.id
    assert renamed.name == "Pizza"
    assert renamed.quality_profile_id == 4
    assert renamed.root_folder == "/movies"
    assert store.decrypt_key(app.id) == RADARR_KEY
