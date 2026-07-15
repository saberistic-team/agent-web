"""Unit tests for admin login limiter secrets and keyed identifiers."""

from __future__ import annotations

import hashlib
import logging
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app import admin_auth
from app.admin_security import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    admin_login_limiter_secrets,
    digest_limiter_identifier,
    validate_admin_auth_security_settings,
    validate_admin_login_limiter_secret,
)
from app.config import Settings, get_settings
from tests.conftest import TEST_LIMITER_SECRET

TEST_SECRET_A = TEST_LIMITER_SECRET
TEST_SECRET_B = "alternate-limiter-secret-32chars-minimum"


def _settings(
    *,
    limiter_secret: str = TEST_SECRET_A,
    previous_secret: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_previous_secret=previous_secret,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    keyed = admin_auth.build_source_rate_limit_key(source, settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert keyed != plain


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_SECRET_A)
    settings_b = _settings(limiter_secret=TEST_SECRET_B)
    source = "203.0.113.1"
    assert admin_auth.build_source_rate_limit_key(source, settings_a) != (
        admin_auth.build_source_rate_limit_key(source, settings_b)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    source = "203.0.113.1"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    secret = admin_login_limiter_secrets(settings)[0]
    payload = "operator"
    source_id = digest_limiter_identifier(LIMITER_DOMAIN_SOURCE, payload, secret)
    account_id = digest_limiter_identifier(LIMITER_DOMAIN_ACCOUNT, payload, secret)
    assert source_id != account_id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short-secret", "must be at least 32 bytes"),
        ("changeme", "must not be a placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_login_limiter_secret(secret, env_name="ADMIN_LOGIN_LIMITER_SECRET")


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET is required"):
        validate_admin_auth_security_settings(settings)


@pytest.mark.unit
def test_rotation_window_includes_previous_secret_identifiers() -> None:
    settings = _settings(limiter_secret=TEST_SECRET_B, previous_secret=TEST_SECRET_A)
    keys = admin_auth.login_limiter_keys(
        submitted_username="operator",
        client_source="203.0.113.5",
        configured_admin_username="operator",
        settings=settings,
    )
    current_only = _settings(limiter_secret=TEST_SECRET_B)
    previous_only = _settings(limiter_secret=TEST_SECRET_A)
    assert admin_auth.build_source_rate_limit_key("203.0.113.5", previous_only) in keys
    assert admin_auth.build_account_rate_limit_key("operator", current_only) in keys
    assert len(keys) >= 3


@pytest.mark.unit
def test_failed_login_audit_spy_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_routes import _record_login_failure

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    candidate = "attacker-supplied-username"

    with patch("app.admin_routes.audit_service.record_login_failure") as audit_mock:
        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = object()
            db_conn.return_value.__exit__.return_value = None
            with patch("app.admin_routes.crm_transaction") as tx:
                tx.return_value.__enter__.return_value = None
                tx.return_value.__exit__.return_value = None
                _record_login_failure(request, reason="invalid_credentials")

    audit_mock.assert_called_once()
    actor = audit_mock.call_args.kwargs["actor_context"].actor
    assert actor == "anonymous"
    assert candidate not in str(audit_mock.call_args.kwargs)


@pytest.mark.unit
def test_login_failure_routes_record_anonymous_actor_for_all_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from tests import test_admin_auth as auth_tests

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", auth_tests.TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash(auth_tests.TEST_PASSWORD))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", auth_tests.TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    store = auth_tests.FakeRateLimitStore()
    candidate = "configured-operator-candidate"

    with auth_tests.shared_rate_limiter(store):
        with auth_tests.mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure") as audit_mock:
                with patch(
                    "app.admin_routes.admin_auth.verify_admin_credentials",
                    return_value=False,
                ):
                    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                        bad_password = auth_tests._login(
                            username=candidate,
                            password="wrong-password",
                        )
                assert bad_password.status_code == 401
                assert audit_mock.call_count == 1
                assert audit_mock.call_args.kwargs["actor_context"].actor == "anonymous"
                assert candidate not in str(audit_mock.call_args.kwargs)

                audit_mock.reset_mock()
                with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                    bad_csrf = auth_tests._login(
                        username=candidate,
                        password="wrong-password",
                    )
                assert bad_csrf.status_code == 400
                assert audit_mock.call_count == 1
                assert audit_mock.call_args.kwargs["actor_context"].actor == "anonymous"
                assert audit_mock.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
def test_failed_login_logs_exclude_candidate_and_secret_material(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from tests import test_admin_auth as auth_tests

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", auth_tests.TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash(auth_tests.TEST_PASSWORD))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", auth_tests.TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    caplog.set_level(logging.INFO)
    candidate = "attacker-log-candidate-xyz"
    store = auth_tests.FakeRateLimitStore()
    with auth_tests.shared_rate_limiter(store):
        with auth_tests.mock_db_connection():
            with patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                return_value=False,
            ):
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    response = auth_tests._login(username=candidate, password="wrong-password")
    assert response.status_code == 401

    forbidden = {candidate, TEST_LIMITER_SECRET, f"src:{candidate}"}
    for record in caplog.records:
        message = record.getMessage()
        for value in forbidden:
            assert value not in message
        for value in forbidden:
            assert value not in str(getattr(record, "__dict__", {}))
