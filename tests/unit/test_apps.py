"""Step 10 unit test: API keys encrypted at rest, CRUD, URL normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import crypto
from app.core.audit import AuditLog
from app.services.apps import (
    API_KEY_MASK,
    KIND_RADARR,
    KIND_SONARR,
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


# --- connection kinds (TV step 2) ---


def _write_legacy_apps_yml(tmp_path: Path, encrypted: str) -> None:
    """An apps.yml exactly as every install before kinds existed wrote it: no `kind`
    key anywhere, and no schema bump, because the field is additive with a default."""
    (tmp_path / "apps.yml").write_text(
        "schema_version: 1\n"
        "apps:\n"
        "  - id: app-legacy\n"
        "    name: Local\n"
        "    url: http://radarr.local:7878\n"
        f"    api_key_encrypted: {encrypted}\n",
        encoding="utf-8",
    )


def test_a_pre_kinds_apps_yml_loads_as_radarr(tmp_path: Path) -> None:
    """The upgrade path. Nobody's existing connections may change meaning, move, or
    need a migration because television arrived."""
    key = crypto.generate_key()
    store = AppsStore(tmp_path, key=key, audit=AuditLog(tmp_path / "audit.jsonl"))
    _write_legacy_apps_yml(tmp_path, crypto.encrypt_field(RADARR_KEY, key))

    loaded = store.list_apps()
    assert [app.id for app in loaded] == ["app-legacy"]
    assert loaded[0].kind == KIND_RADARR
    assert loaded[0].kind_name == "Radarr"
    # And it is still the connection every movie action reaches for.
    assert store.primary_id() == "app-legacy"
    assert store.decrypt_key("app-legacy") == RADARR_KEY


def test_an_unknown_stored_kind_reads_as_radarr_rather_than_failing(tmp_path: Path) -> None:
    """Read-tolerant, like users._validated_theme: a hand-edited or future-build file
    renders as the default rather than refusing to load."""
    key = crypto.generate_key()
    store = AppsStore(tmp_path, key=key, audit=AuditLog(tmp_path / "audit.jsonl"))
    (tmp_path / "apps.yml").write_text(
        "schema_version: 1\n"
        "apps:\n"
        "  - id: app-odd\n"
        "    name: Odd\n"
        "    url: http://odd.local:7878\n"
        f"    api_key_encrypted: {crypto.encrypt_field(RADARR_KEY, key)}\n"
        "    kind: lidarr\n",
        encoding="utf-8",
    )

    assert store.get("app-odd").kind == KIND_RADARR


def test_adding_an_unknown_kind_is_refused(store: AppsStore) -> None:
    """Write-strict. The caller is a form submission, so a kind this build does not
    ship is a bug or an attack, not something to quietly accept."""
    with pytest.raises(InvalidAppError):
        store.add(name="Odd", url="odd.local:7878", api_key=RADARR_KEY, kind="lidarr")


def test_listing_defaults_to_radarr_so_a_sonarr_never_reaches_a_movie_menu(
    store: AppsStore,
) -> None:
    """The regression contract. Every caller that predates kinds — the Add menus, the
    target caret, the where-does-this-film-live resolution — calls list_apps() bare and
    means the Radarr ones. A Sonarr appearing there would offer somewhere to send a FILM
    that cannot hold one."""
    radarr = store.add(name="Local", url="radarr.local:7878", api_key=RADARR_KEY)
    sonarr = store.add(
        name="Sonarr", url="sonarr.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR
    )

    assert [app.id for app in store.list_apps()] == [radarr.id]
    assert [app.id for app in store.list_apps(KIND_SONARR)] == [sonarr.id]
    # Settings is the one caller that wants every kind.
    assert {app.id for app in store.list_apps(kind=None)} == {radarr.id, sonarr.id}
    # And a lookup by id still finds either, whatever kind it is.
    assert store.get(sonarr.id).kind == KIND_SONARR


def test_each_kind_has_its_own_primary(store: AppsStore) -> None:
    radarr = store.add(name="Local", url="radarr.local:7878", api_key=RADARR_KEY)
    sonarr = store.add(
        name="Sonarr", url="sonarr.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR
    )

    assert store.primary_id() == radarr.id
    assert store.primary_id(KIND_SONARR) == sonarr.id


def test_choosing_a_sonarr_primary_leaves_the_radarr_one_alone(store: AppsStore) -> None:
    """Which Sonarr takes a series and which Radarr the weekly run talks to are separate
    questions. Answering one must not silently re-answer the other."""
    first_radarr = store.add(name="Local", url="a.local:7878", api_key=RADARR_KEY)
    store.add(name="Remote 4K", url="b.local:7878", api_key=RADARR_KEY)
    store.set_primary(first_radarr.id)
    store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    second_sonarr = store.add(
        name="Sonarr 4K", url="s2.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR
    )

    store.set_primary(second_sonarr.id)

    assert store.primary_id() == first_radarr.id
    assert store.primary_id(KIND_SONARR) == second_sonarr.id
    # Exactly one winner within each kind, and no cross-kind demotion.
    assert [app.primary for app in store.list_apps()] == [True, False]
    assert [app.primary for app in store.list_apps(KIND_SONARR)] == [False, True]


def test_removing_a_primary_promotes_within_its_own_kind(store: AppsStore) -> None:
    """Promoting across kinds would hand the Sonarr primacy to a Radarr, and the next
    series add would post a series to a server that only knows about films."""
    radarr = store.add(name="Local", url="a.local:7878", api_key=RADARR_KEY)
    store.set_primary(radarr.id)
    first_sonarr = store.add(
        name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR
    )
    second_sonarr = store.add(
        name="Sonarr 4K", url="s2.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR
    )
    store.set_primary(second_sonarr.id)

    store.remove(second_sonarr.id)

    assert store.primary_id(KIND_SONARR) == first_sonarr.id
    assert store.get(first_sonarr.id).primary is True
    assert store.primary_id() == radarr.id


def test_removing_the_last_sonarr_leaves_no_sonarr_primary(store: AppsStore) -> None:
    radarr = store.add(name="Local", url="a.local:7878", api_key=RADARR_KEY)
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    store.set_primary(sonarr.id)

    store.remove(sonarr.id)

    assert store.primary_id(KIND_SONARR) is None
    # The Radarr is untouched — not promoted into a role it cannot fill.
    assert store.get(radarr.id).primary is False


def test_sonarr_only_defaults_round_trip(store: AppsStore) -> None:
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)

    store.set_defaults(
        sonarr.id,
        quality_profile_id=4,
        root_folder="/tv",
        series_type="anime",
        season_folders=True,
        search_on_add=False,
    )

    stored = store.get(sonarr.id)
    assert stored.series_type == "anime"
    assert stored.season_folders is True
    # False is a value, not an absence: a toggle turned off must survive.
    assert stored.search_on_add is False


def test_a_caller_that_knows_nothing_of_series_options_cannot_erase_them(
    store: AppsStore,
) -> None:
    """None means "leave the stored one", the same contract a blank API key field has.
    Every Radarr card and every movie flow calls set_defaults with two arguments."""
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    store.set_defaults(
        sonarr.id, quality_profile_id=4, root_folder="/tv",
        series_type="daily", season_folders=False, search_on_add=True,
    )

    store.set_defaults(sonarr.id, quality_profile_id=7, root_folder="/tv2")

    stored = store.get(sonarr.id)
    assert (stored.quality_profile_id, stored.root_folder) == (7, "/tv2")
    assert stored.series_type == "daily"
    assert stored.season_folders is False
    assert stored.search_on_add is True


def test_an_unknown_series_type_is_refused(store: AppsStore) -> None:
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    with pytest.raises(InvalidAppError):
        store.set_defaults(
            sonarr.id, quality_profile_id=None, root_folder=None, series_type="cartoons"
        )


def test_a_radarr_connection_carries_no_series_options(store: AppsStore, tmp_path: Path) -> None:
    """A film has no seasons. The keys are absent from the file rather than stored as
    meaningless nulls."""
    radarr = store.add(name="Local", url="a.local:7878", api_key=RADARR_KEY)
    store.set_defaults(radarr.id, quality_profile_id=4, root_folder="/movies")

    raw = (tmp_path / "apps.yml").read_text(encoding="utf-8")
    assert "series_type" not in raw
    assert "season_folders" not in raw
    assert store.get(radarr.id).series_type is None


def test_building_a_radarr_client_for_a_sonarr_is_refused(store: AppsStore) -> None:
    """Sonarr serves /api/v3/system/status too, so a Radarr client pointed at one would
    answer and the health dot would go green — then the first `movie` call would fail
    somewhere far from the cause."""
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    with pytest.raises(InvalidAppError):
        store.build_client(sonarr.id, tls_verify=True, ca_file=None)


def test_the_public_view_names_the_kind_and_still_masks_the_key(store: AppsStore) -> None:
    sonarr = store.add(name="Sonarr", url="s1.local:8989", api_key=RADARR_KEY, kind=KIND_SONARR)
    view = store.get(sonarr.id).public()

    assert view["kind"] == KIND_SONARR
    assert view["kind_name"] == "Sonarr"
    assert view["api_key_mask"] == API_KEY_MASK
    assert RADARR_KEY not in str(view)
