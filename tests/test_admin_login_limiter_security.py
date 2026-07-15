"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_security import validate_admin_login_limiter_secret, validate_admin_security_config
from app.config import Settings, get_settings
from app.main import app
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
)

PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"

client = TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings_with_secrets(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    previous_secret: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_previous_secret=previous_secret,
    )


def _plain_sha256_limiter_key(domain: str, material: str) -> str:
    payload = f"{domain}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source = "203.0.113.10"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256_limiter_key("src", source)
    assert account_key != _plain_sha256_limiter_key("acct", account)


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secrets(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings_with_secrets(limiter_secret=PREVIOUS_LIMITER_SECRET)
    source = "203.0.113.10"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = get_settings()
    source = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second
    assert len(first) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", first)


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = get_settings()
    payload = "203.0.113.10"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name", "pattern"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET", "required"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET", "at least 32"),
        ("this-is-a-placeholder-secret-32chars!!", "ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match=pattern):
        validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_startup_validation_rejects_matching_current_and_previous_secret() -> None:
    settings = _settings_with_secrets(
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_variants() -> None:
    settings = _settings_with_secrets(
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    previous_only = _settings_with_secrets(limiter_secret=PREVIOUS_LIMITER_SECRET)
    previous_source = admin_auth.build_source_rate_limit_key("203.0.113.10", previous_only)
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_cleanup_eligible_for_expired_previous_key_rows() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 2
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )

    assert deleted == 2
    execute_sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in execute_sql
    assert "locked_until IS NULL OR locked_until <" in execute_sql


@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_unknown_username() -> None:
    captured: list[dict[str, Any]] = []

    def _capture_append(_conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": len(captured), **kwargs}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.repositories.postgres.PostgresAuditEventRepository.append",
                side_effect=_capture_append,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "attacker-supplied-name",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401

    assert len(captured) == 1
    event = captured[0]
    assert event["actor"] == "anonymous"
    assert "attacker-supplied-name" not in json.dumps(event)


@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_wrong_password() -> None:
    captured: list[dict[str, Any]] = []

    def _capture_append(_conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": len(captured), **kwargs}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.repositories.postgres.PostgresAuditEventRepository.append",
                side_effect=_capture_append,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401

    assert captured[0]["actor"] == "anonymous"
    assert TEST_USERNAME not in json.dumps(captured[0].get("metadata"))


@pytest.mark.integration
def test_invalid_flow_failure_audit_stays_anonymous() -> None:
    with mock_db_connection():
        with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 400
            failure_audit.assert_called_once()
            actor_context = failure_audit.call_args.kwargs["actor_context"]
            assert actor_context.actor == "anonymous"


@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 303
                    success_audit.assert_called_once()
                    assert (
                        success_audit.call_args.kwargs["actor_context"].actor
                        == TEST_USERNAME
                    )
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_secret_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    candidate = "attacker-log-candidate"
    secret = get_settings().admin_login_limiter_secret
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": candidate,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 401

    combined = caplog.text
    assert candidate not in combined
    assert secret not in combined


@pytest.mark.unit
def test_startup_validation_accepts_configured_limiter_secret() -> None:
    validate_admin_security_config(get_settings())


@pytest.mark.unit
def test_record_login_failure_service_keeps_anonymous_actor_only() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    actor = ActorContext(actor="anonymous", correlation_id="corr-242")

    audit_service.record_login_failure(
        conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )

    event = repo.append.call_args.kwargs
    assert event["actor"] == "anonymous"
    assert event["summary_after"] == {"reason": "invalid_credentials"}
