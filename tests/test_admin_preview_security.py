"""Unit tests for admin preview loopback security (issue #330)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.admin_preview_security import (
    AdminPreviewConfigError,
    LoopbackOriginError,
    compute_admin_preview_enabled,
    is_loopback_bind_host,
    validate_admin_preview_config,
    validate_loopback_base_url,
    validate_preview_server_bind,
)
from app.app_environment import AppEnvironment
from app.config import Settings, get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)


def _settings(**overrides: object) -> Settings:
    base = get_settings()
    values = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "plausible_domain": base.plausible_domain,
        "plausible_api_key": base.plausible_api_key,
        "analytics_environment": base.analytics_environment,
        "admin_username": base.admin_username,
        "admin_password_hash": base.admin_password_hash,
        "admin_session_secret": base.admin_session_secret,
        "app_environment": base.app_environment,
        "server_bind_host": base.server_bind_host,
        "admin_preview_mode": base.admin_preview_mode,
        "admin_preview_enabled": base.admin_preview_enabled,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:8765", "http://localhost:8765"),
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://127.0.0.2:8765", "http://127.0.0.2:8765"),
        ("http://[::1]:8765", "http://[::1]:8765"),
        ("http://localhost.", "http://localhost"),
    ],
)
def test_validate_loopback_base_url_accepts_local_origins(
    base_url: str,
    expected: str,
) -> None:
    assert validate_loopback_base_url(base_url) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "https://saberistic.com",
        "https://staging.saberistic.com",
        "https://evil.example.com",
        "http://localhost.example.com:8765",
        "https://saberistic.com.evil",
        "https://evil-saberistic.com",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:",
        "http://*/",
        "http:///admin",
        "http://127.0.0.1:8765#fragment",
    ],
)
def test_validate_loopback_base_url_rejects_unsafe_origins(base_url: str) -> None:
    with pytest.raises(LoopbackOriginError):
        validate_loopback_base_url(base_url)


@pytest.mark.unit
def test_missing_base_url_cannot_enable_preview_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    settings = get_settings()
    assert settings.admin_preview_enabled is False
    with pytest.raises(AdminPreviewConfigError):
        validate_admin_preview_config(settings)


@pytest.mark.unit
def test_default_localhost_base_url_cannot_enable_preview_in_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    settings = get_settings()
    assert settings.admin_preview_enabled is False


@pytest.mark.unit
def test_host_header_spoofing_does_not_change_preview_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "preview")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    settings_before = get_settings()
    response = client.get("/admin", headers={"host": "127.0.0.1:8765"})
    settings_after = get_settings()
    assert settings_before.admin_preview_enabled is settings_after.admin_preview_enabled
    assert response.status_code == 200


@pytest.mark.unit
def test_preview_with_public_bind_fails_validation() -> None:
    settings = _settings(
        admin_preview_mode=True,
        app_environment=AppEnvironment.PREVIEW,
        base_url="http://127.0.0.1:8765",
        server_bind_host="0.0.0.0",
        admin_preview_enabled=False,
    )
    with pytest.raises(AdminPreviewConfigError, match="public interface"):
        validate_admin_preview_config(settings)


@pytest.mark.unit
def test_production_always_disables_preview_even_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    settings = get_settings()
    assert settings.admin_preview_enabled is False
    with pytest.raises(AdminPreviewConfigError):
        validate_admin_preview_config(settings)


@pytest.mark.unit
def test_compute_admin_preview_enabled_requires_all_invariants() -> None:
    assert compute_admin_preview_enabled(
        admin_preview_mode=True,
        app_environment=AppEnvironment.PREVIEW,
        base_url="http://127.0.0.1:8765",
        server_bind_host="127.0.0.1",
    )
    assert not compute_admin_preview_enabled(
        admin_preview_mode=True,
        app_environment=AppEnvironment.PRODUCTION,
        base_url="http://127.0.0.1:8765",
        server_bind_host="127.0.0.1",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("127.0.0.8", True),
        ("0.0.0.0", False),
        ("::", False),
        ("203.0.113.1", False),
        ("", False),
    ],
)
def test_is_loopback_bind_host(host: str, expected: bool) -> None:
    assert is_loopback_bind_host(host) is expected


@pytest.mark.unit
def test_validate_preview_server_bind_rejects_public_addresses() -> None:
    with pytest.raises(AdminPreviewConfigError):
        validate_preview_server_bind("0.0.0.0")
