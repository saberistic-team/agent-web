"""Direct unit coverage for app.admin_security helpers not exercised elsewhere.

Complements tests/test_admin_login_limiter_identifiers.py (issue #242), which
covers the end-to-end login/limiter/audit behavior; these tests target the
small early-return and helper branches in app/admin_security.py directly.
"""

from __future__ import annotations

import pytest

from app.admin_security import (
    AdminSecurityConfigError,
    compare_limiter_digests,
    digest_limiter_key,
    plain_sha256_limiter_key,
    validate_admin_security_config,
    validate_limiter_secret,
)
from app.config import get_settings


@pytest.mark.unit
def test_validate_limiter_secret_optional_allows_empty_when_not_required() -> None:
    validate_limiter_secret("", env_name="ADMIN_LOGIN_LIMITER_SECRET", required=False)


@pytest.mark.unit
def test_validate_admin_security_config_skips_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = get_settings()
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_validate_admin_security_config_skips_when_admin_identity_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    settings = get_settings()
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_validate_admin_security_config_accepts_distinct_rotation_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "session-secret-32chars-minimum!!")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "current-limiter-secret-32chars-min!!")
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", "previous-limiter-secret-32chars-min!!"
    )
    settings = get_settings()
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_validate_limiter_secret_raises_admin_security_config_error() -> None:
    with pytest.raises(AdminSecurityConfigError):
        validate_limiter_secret("", env_name="ADMIN_LOGIN_LIMITER_SECRET")


@pytest.mark.unit
def test_plain_sha256_limiter_key_differs_from_hmac_digest() -> None:
    plain = plain_sha256_limiter_key("src", "203.0.113.10")
    keyed = digest_limiter_key(domain="src", material="203.0.113.10", secret="secret-32chars-minimum-length!!!")
    assert plain != keyed


@pytest.mark.unit
def test_compare_limiter_digests_constant_time_equality() -> None:
    digest = digest_limiter_key(domain="src", material="203.0.113.10", secret="secret-32chars-minimum-length!!!")
    assert compare_limiter_digests(digest, digest)
    assert not compare_limiter_digests(digest, "different")
