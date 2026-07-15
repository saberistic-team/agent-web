"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app import admin_auth
from app.admin_security import (
    ADMIN_LOGIN_LIMITER_SECRET_MIN_LENGTH,
    validate_admin_login_limiter_secret,
    validate_admin_security_secrets,
)
from app.config import Settings, get_settings
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    client,
    mock_db_connection,
    shared_rate_limiter,
    _fetch_login_form,
    _login,
)

ALT_LIMITER_SECRET = "alternate-login-limiter-secret-32chars!!"
PREVIOUS_LIMITER_SECRET = "previous-login-limiter-secret-32chars!!"


def _plain_sha256_limiter_digest(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    fields = {
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
        "admin_login_limiter_secret": base.admin_login_limiter_secret,
        "admin_login_limiter_secret_previous": base.admin_login_limiter_secret_previous,
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source = "203.0.113.50"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256_limiter_digest("src", source.lower())
    assert account_key != _plain_sha256_limiter_digest("acct", account.lower())


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings = get_settings()
    alt = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    source = "203.0.113.50"
    current = admin_auth.build_source_rate_limit_key(source, settings)
    alternate = admin_auth.build_source_rate_limit_key(source, alt)
    assert current != alternate


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = get_settings()
    payload = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "required"),
        ("short-secret", "at least"),
        ("changeme", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_limiter_secret_validation_accepts_strong_material() -> None:
    secret = "a" * ADMIN_LOGIN_LIMITER_SECRET_MIN_LENGTH
    assert validate_admin_login_limiter_secret(secret) == secret


@pytest.mark.unit
def test_startup_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_secrets(get_settings())


@pytest.mark.unit
def test_rotation_check_keys_include_previous_secret_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    keys = admin_auth.login_limiter_rotation_check_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.50",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.50", settings, secret=PREVIOUS_LIMITER_SECRET
    )
    current_account = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    previous_account = admin_auth.build_account_rate_limit_key(
        TEST_USERNAME, settings, secret=PREVIOUS_LIMITER_SECRET
    )
    assert current_source in keys
    assert previous_source in keys
    assert current_account in keys
    assert previous_account in keys


@pytest.mark.unit
def test_rotation_honors_previous_key_lockout_without_incrementing_it(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_source = admin_auth.build_source_rate_limit_key(
        "testclient", settings, secret=PREVIOUS_LIMITER_SECRET
    )
    locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    rate_limit_store.rows[previous_source] = {
        "failure_count": 5,
        "window_started_at": datetime.now(timezone.utc),
        "locked_until": locked_until,
        "updated_at": datetime.now(timezone.utc),
    }

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )

    assert response.status_code == 429
    current_source = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert current_source not in rate_limit_store.rows


@pytest.mark.unit
def test_successful_login_clears_previous_account_limiter_keys(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_account = admin_auth.build_account_rate_limit_key(
        TEST_USERNAME, settings, secret=PREVIOUS_LIMITER_SECRET
    )
    current_account = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    now = datetime.now(timezone.utc)
    for key in (previous_account, current_account):
        rate_limit_store.rows[key] = {
            "failure_count": 2,
            "window_started_at": now,
            "locked_until": None,
            "updated_at": now,
        }

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            login = _login()
            assert login.status_code == 303

    assert previous_account not in rate_limit_store.rows
    assert current_account not in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = _login(username="attacker-candidate", password="wrong-password")

    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    event_blob = json.dumps(captured, default=str)
    assert "attacker-candidate" not in event_blob


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = _login(password="wrong-password")

    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "attacker-candidate",
                        "password": TEST_PASSWORD,
                        "csrf_token": csrf_token + "tampered",
                    },
                    cookies=cookies,
                )

    assert response.status_code == 400
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_success",
                side_effect=_capture,
            ):
                response = _login()

    assert response.status_code == 303
    assert captured["actor_context"].actor == TEST_USERNAME
    assert isinstance(captured["session_id"], int)


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_do_not_leak_candidates_or_secrets(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    candidate = "attacker-candidate"
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = _login(username=candidate, password="wrong-password")

    assert response.status_code == 401
    log_blob = caplog.text
    assert candidate not in log_blob
    assert TEST_LIMITER_SECRET not in log_blob
    assert "203.0.113" not in log_blob


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                assert _login(password="wrong").status_code == 401
                lockout = _login(password="wrong")

    assert lockout.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "rate_limited"
