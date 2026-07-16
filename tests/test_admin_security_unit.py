"""Unit tests for admin security configuration helpers."""

from __future__ import annotations

import pytest

from app.admin_security import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    AdminSecurityConfigError,
    digest_limiter_key,
    validate_admin_security_config,
    validate_limiter_secret,
)
from app.config import get_settings


@pytest.mark.unit
def test_validate_admin_security_config_accepts_strong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "session-secret-32chars-minimum!!")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "limiter-secret-32chars-minimum!!")
    validate_admin_security_config(get_settings())


@pytest.mark.unit
def test_validate_admin_security_config_exports_domain_constants() -> None:
    assert LIMITER_DOMAIN_SOURCE == "src"
    assert LIMITER_DOMAIN_ACCOUNT == "acct"
    assert digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.1",
        secret="limiter-secret-32chars-minimum!!",
    ) != digest_limiter_key(
        domain=LIMITER_DOMAIN_ACCOUNT,
        material="203.0.113.1",
        secret="limiter-secret-32chars-minimum!!",
    )


@pytest.mark.unit
def test_validate_limiter_secret_optional_allows_empty() -> None:
    validate_limiter_secret("", env_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", required=False)


@pytest.mark.unit
def test_validate_admin_security_config_rejects_matching_rotation_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "limiter-secret-32chars-minimum!!"
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "session-secret-32chars-minimum!!")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", secret)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", secret)
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_security_config(get_settings())
