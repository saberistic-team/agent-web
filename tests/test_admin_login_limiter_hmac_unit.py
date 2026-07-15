"""Tests for HMAC login limiter identifiers and anonymous failed-login audit actors."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service
from app.admin_secrets import validate_admin_login_limiter_secret, validate_admin_security_secrets
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    mock_db_connection,
    shared_rate_limiter,
    _login,
)

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"
OTHER_LIMITER_SECRET = "other-limiter-secret-32chars-minimum"
CANDIDATE_USERNAME = "attacker-supplied-name"
CLIENT_SOURCE = "203.0.113.50"


def _settings(**overrides: Any) -> Settings:
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
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_secret_previous": "",
    }
    values.update(overrides)
    return Settings(**values)


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)

    assert source_key != _plain_sha256_limiter_key("src", CLIENT_SOURCE)
    assert account_key != _plain_sha256_limiter_key("acct", TEST_USERNAME)
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=OTHER_LIMITER_SECRET)

    key_a = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "required"),
        ("short", "32 characters"),
        ("changeme", "32 characters"),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "low-entropy"),
    ],
)
def test_limiter_secret_validation_rejects_weak_values(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_login_limiter_secret(value)


@pytest.mark.unit
def test_validate_admin_security_secrets_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)

    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_secrets(get_settings())


@pytest.mark.unit
def test_rotation_throttle_checks_previous_key_while_writes_use_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_secret = "previous-limiter-secret-32chars-minimum"
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    settings = get_settings()

    current_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    previous_key = admin_auth._digest_limiter_key(
        previous_secret.encode("utf-8"),
        admin_auth.LIMITER_KEY_DOMAIN_SRC,
        CLIENT_SOURCE,
    )
    assert current_key != previous_key

    throttle_keys = admin_auth.login_limiter_throttle_keys(
        submitted_username=TEST_USERNAME,
        client_source=CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert current_key in throttle_keys
    assert previous_key in throttle_keys
    assert admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    ) == (
        current_key,
        admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings),
    )


@pytest.mark.unit
def test_previous_secret_lock_blocks_admission_without_incrementing_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_secret = "previous-limiter-secret-32chars-minimum"
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    settings = get_settings()
    previous_key = admin_auth._digest_limiter_key(
        previous_secret.encode("utf-8"),
        admin_auth.LIMITER_KEY_DOMAIN_SRC,
        CLIENT_SOURCE,
    )
    current_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    now = datetime.now(timezone.utc)
    locked_until = now + timedelta(minutes=15)

    conn = MagicMock()

    def _is_throttled(
        _conn: Any,
        *,
        limiter_key: str,
        now: datetime,
    ) -> bool:
        return limiter_key == previous_key

    with (
        patch("app.admin_auth.db.db_connection") as db_conn,
        patch("app.admin_auth.db.is_admin_login_throttled", side_effect=_is_throttled),
        patch("app.admin_auth.db.try_admit_admin_login") as admit,
    ):
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
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
            "client": (CLIENT_SOURCE, 12345),
            "server": ("testserver", 80),
        }
        from starlette.requests import Request

        request = Request(scope)
        result = admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=TEST_USERNAME,
        )

    assert not result.admitted
    assert result.already_locked
    admit.assert_not_called()


@contextmanager
def _mock_login_db() -> Generator[MagicMock, None, None]:
    with mock_db_connection() as conn:
        yield conn


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.mark.unit
def test_record_login_failure_helper_uses_anonymous_actor() -> None:
    from app.admin_routes import _record_login_failure

    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "corr-anon"

    with (
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.crm_transaction"),
        patch("app.admin_routes.audit_service.record_login_failure") as audit,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        _record_login_failure(request, reason="invalid_credentials")

    audit.assert_called_once()
    actor_context = audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert audit.call_args.kwargs["reason"] == "invalid_credentials"
    assert "attempted_username" not in audit.call_args.kwargs


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor_only(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with _mock_login_db():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.audit_service.record_login_failure") as spy:
                    response = _login(username=CANDIDATE_USERNAME, password="wrong-password")
                    assert response.status_code == 401
                    spy.assert_called_once()
                    assert spy.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert spy.call_args.kwargs["reason"] == "invalid_credentials"
                    assert CANDIDATE_USERNAME not in str(spy.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with _mock_login_db():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.audit_service.record_login_failure") as spy:
                    response = _login(password="wrong-password")
                    assert response.status_code == 401
                    spy.assert_called_once()
                    assert spy.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert spy.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with _mock_login_db():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes.audit_service.record_login_failure") as spy:
                    response = _login(username=CANDIDATE_USERNAME, password="wrong-password")
                    assert response.status_code == 400
                    spy.assert_called_once()
                    assert spy.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert spy.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
def test_record_login_failure_repository_receives_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-repo"}
    conn = MagicMock()
    audit_service.record_login_failure(
        conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-repo"),
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert append_kwargs["action"] == audit_service.ACTION_AUTH_LOGIN_FAILURE
    assert append_kwargs["summary_after"]["reason"] == "invalid_credentials"
    assert CANDIDATE_USERNAME not in str(append_kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with _mock_login_db():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.db.create_admin_session",
                    return_value=42,
                ):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        login = _login()
                        assert login.status_code == 303
                        success_audit.assert_called_once()
                        assert (
                            success_audit.call_args.kwargs["actor_context"].actor
                            == TEST_USERNAME
                        )
                        assert success_audit.call_args.kwargs["session_id"] is not None


@pytest.mark.unit
def test_limiter_logs_do_not_include_candidates_or_secrets(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "1")
    caplog.set_level(logging.INFO, logger="app.admin_auth")

    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
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
            "client": (CLIENT_SOURCE, 12345),
            "server": ("testserver", 80),
        }
        from starlette.requests import Request

        request = Request(scope)
        settings = get_settings()
        admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=CANDIDATE_USERNAME,
        )

    combined = caplog.text
    assert CANDIDATE_USERNAME not in combined
    assert CLIENT_SOURCE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined
