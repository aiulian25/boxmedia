"""Step 2 test: config fails fast on bad input, loads on good input."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import SESSION_SECRET_MIN_LENGTH, Settings

VALID_SECRET = "x" * SESSION_SECRET_MIN_LENGTH
KEY_FILE = "/secrets/boxmedia.key"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "session_secret": VALID_SECRET,
        "encryption_key_file": KEY_FILE,
    }
    base.update(overrides)
    # _env_file=None so a developer's real .env never leaks into the test.
    return Settings(_env_file=None, **base)


def test_missing_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, encryption_key_file=KEY_FILE)


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(session_secret="tooshort")


def test_valid_config_loads_with_defaults() -> None:
    settings = _settings()
    assert settings.port == 8686
    assert settings.host_port == 58546
    assert settings.outbound_tls_verify is True
    assert settings.url_base == ""


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 98546])
def test_out_of_range_ports_rejected(bad_port: int) -> None:
    with pytest.raises(ValidationError):
        _settings(port=bad_port)
    with pytest.raises(ValidationError):
        _settings(host_port=bad_port)


def test_url_base_normalized() -> None:
    assert _settings(url_base="/boxmedia/").url_base == "/boxmedia"
    assert _settings(url_base="").url_base == ""


def test_url_base_without_leading_slash_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(url_base="boxmedia")


def test_public_summary_excludes_secrets() -> None:
    summary = _settings().public_summary()
    assert VALID_SECRET not in summary.values()
    assert "session_secret" not in summary


def test_boxoffice_url_default_matches_scraper_constant() -> None:
    # The URL is deliberately duplicated (core must not import services). Pin the two
    # copies equal so a change to one without the other is caught here.
    from app.services.boxoffice import BOM_WEEKLY_URL

    assert _settings().boxoffice_url == BOM_WEEKLY_URL


def test_derived_paths_follow_data_dir() -> None:
    settings = _settings(data_dir="/srv/box")
    assert str(settings.config_dir) == "/srv/box/config"
    assert str(settings.backups_dir) == "/srv/box/backups"


def test_blank_tls_ca_file_is_none() -> None:
    # A blank BM_TLS_CA_FILE must NOT become Path(".") — that directory breaks SSL
    # context creation on every outbound Radarr call.
    assert _settings(tls_ca_file="").tls_ca_file is None
    assert _settings(tls_ca_file="   ").tls_ca_file is None
    assert _settings().tls_ca_file is None
    assert str(_settings(tls_ca_file="/ca.pem").tls_ca_file) == "/ca.pem"


def test_session_lifetime_knobs() -> None:
    assert _settings().session_ttl_hours == 12  # default
    assert _settings().session_idle_minutes == 0  # idle timeout off by default
    assert _settings(session_ttl_hours=1, session_idle_minutes=30).session_idle_minutes == 30
    with pytest.raises(ValidationError):
        _settings(session_ttl_hours=0)  # a zero-length session would lock the admin out
    with pytest.raises(ValidationError):
        _settings(session_idle_minutes=-1)


def test_login_lockout_policy_knobs() -> None:
    assert _settings().login_max_attempts == 5  # ships as the limiter's own default
    assert _settings().login_window_seconds == 300
    assert _settings().login_lock_seconds == 900
    tightened = _settings(login_max_attempts=3, login_window_seconds=600,
                          login_lock_seconds=3600)
    assert (tightened.login_max_attempts, tightened.login_lock_seconds) == (3, 3600)
    # Lower bounds keep a typo from effectively disabling brute-force protection.
    for bad in ({"login_max_attempts": 0}, {"login_window_seconds": 5},
                {"login_lock_seconds": 10}):
        with pytest.raises(ValidationError):
            _settings(**bad)
