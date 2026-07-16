"""Unit tests for admin preview loopback-only security invariants (#330)."""

from __future__ import annotations

import pytest

from app.admin_preview_security import (
    AdminPreviewConfigError,
    is_loopback_bind_host,
    is_loopback_origin,
    resolve_admin_preview_enabled,
    validate_admin_preview_config,
    validate_preview_bind_host,
)
from app.app_environment import AppEnvironment
from app.config import Settings, get_settings
from tests.conftest import enable_admin_preview_env


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://localhost.:8000",
        "http://127.0.0.1:8765",
        "http://127.2.3.4:9000",
        "http://[::1]:8765",
    ],
)
def test_loopback_origins_are_accepted(base_url: str) -> None:
    assert is_loopback_origin(base_url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "https://saberistic.com",
        "https://staging.saberistic.com",
        "https://evil.example.com",
        "http://localhost.example.com:8000",
        "https://saberistic.com.evil",
        "https://evil-saberistic.com",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:99999",
        "ftp://127.0.0.1:8000",
        "http://",
        "http://*:8000",
    ],
)
def test_non_loopback_origins_are_rejected(base_url: str) -> None:
    assert not is_loopback_origin(base_url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bind_host",
    ["127.0.0.1", "::1", "[::1]", "localhost"],
)
def test_loopback_bind_hosts_are_accepted(bind_host: str) -> None:
    assert is_loopback_bind_host(bind_host)
    validate_preview_bind_host(bind_host)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bind_host",
    ["0.0.0.0", "::", "203.0.113.1", "10.0.0.1", ""],
)
def test_public_bind_hosts_are_rejected(bind_host: str) -> None:
    assert not is_loopback_bind_host(bind_host)
    with pytest.raises(AdminPreviewConfigError):
        validate_preview_bind_host(bind_host)


@pytest.mark.unit
def test_missing_base_url_cannot_enable_preview_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("BASE_URL", raising=False)

    with pytest.raises(AdminPreviewConfigError):
        get_settings()


@pytest.mark.unit
def test_default_localhost_base_url_cannot_enable_preview_in_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("BASE_URL", raising=False)

    with pytest.raises(AdminPreviewConfigError):
        get_settings()


@pytest.mark.unit
def test_request_host_header_does_not_alter_preview_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")

    with pytest.raises(AdminPreviewConfigError):
        get_settings()

    enable_admin_preview_env(monkeypatch)
    settings = get_settings()
    assert settings.admin_preview_enabled is True
    assert settings.base_url == "http://127.0.0.1:8765"


@pytest.mark.unit
def test_preview_with_public_bind_fails_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch, bind_host="0.0.0.0")

    with pytest.raises(AdminPreviewConfigError):
        get_settings()


@pytest.mark.unit
def test_production_always_disables_preview_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")

    with pytest.raises(AdminPreviewConfigError):
        get_settings()


@pytest.mark.unit
def test_resolve_admin_preview_enabled_requires_explicit_development_env() -> None:
    with pytest.raises(AdminPreviewConfigError):
        resolve_admin_preview_enabled(
            admin_preview_mode=True,
            app_environment=AppEnvironment.PRODUCTION,
            base_url="http://127.0.0.1:8765",
            server_bind_host="127.0.0.1",
        )


@pytest.mark.unit
def test_resolve_admin_preview_enabled_succeeds_for_valid_contract() -> None:
    assert resolve_admin_preview_enabled(
        admin_preview_mode=True,
        app_environment=AppEnvironment.DEVELOPMENT,
        base_url="http://127.0.0.1:8765",
        server_bind_host="127.0.0.1",
    )


@pytest.mark.unit
def test_validate_admin_preview_config_is_noop_when_preview_disabled() -> None:
    validate_admin_preview_config(
        admin_preview_mode=False,
        app_environment=AppEnvironment.PRODUCTION,
        base_url="https://saberistic.com",
        server_bind_host="0.0.0.0",
    )


@pytest.mark.unit
def test_settings_constructed_explicitly_without_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SERVER_BIND_HOST", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)

    settings = Settings(
        database_url="",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url="https://saberistic.com",
        analytics_environment="production",
        app_environment=AppEnvironment.PRODUCTION,
        admin_username="",
        admin_password_hash="",
        admin_session_secret="",
        admin_preview_mode=True,
        admin_preview_enabled=False,
        server_bind_host="127.0.0.1",
    )
    assert settings.admin_preview_enabled is False
