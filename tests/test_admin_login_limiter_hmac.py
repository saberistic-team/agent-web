"""Tests for HMAC login limiter identifiers and anonymous failed-login actors."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def limiter_hmac_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _plain_sha256(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source = "203.0.113.50"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256("src", source.strip().lower())
    assert account_key != _plain_sha256("acct", account.strip().lower())


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = get_settings()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_ALT)
    settings_b = get_settings()
    material = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_across_calls() -> None:
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
def test_limiter_secret_validation_rejects_missing() -> None:
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError, match="required"):
        admin_auth.validate_admin_login_limiter_secret("")


@pytest.mark.unit
def test_limiter_secret_validation_rejects_short() -> None:
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError, match="at least"):
        admin_auth.validate_admin_login_limiter_secret("short")


@pytest.mark.unit
def test_limiter_secret_validation_rejects_placeholder() -> None:
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError, match="placeholder"):
        admin_auth.validate_admin_login_limiter_secret(
            "changeme-changeme-changeme-changeme-changeme"
        )


@pytest.mark.unit
def test_limiter_previous_secret_validation_rejects_short() -> None:
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError, match="at least"):
        admin_auth.validate_admin_login_limiter_secret(
            "short-prev",
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )


@pytest.mark.unit
def test_startup_validation_fails_for_missing_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError, match="required"):
        admin_auth.validate_admin_login_limiter_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    source_keys = admin_auth.build_source_rate_limit_keys("203.0.113.50", settings)
    assert len(source_keys) == 2
    assert source_keys[0] != source_keys[1]


@pytest.mark.unit
def test_rotation_deduplicates_when_previous_matches_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_keys = admin_auth.build_source_rate_limit_keys("203.0.113.50", settings)
    assert source_keys == (admin_auth.build_source_rate_limit_key("203.0.113.50", settings),)


@pytest.mark.integration
def test_rotation_previous_key_rows_remain_eligible_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.migrations.runner import apply_migrations
    from tests.test_admin_login_rate_limit_integration import (
        _connect,
        _require_database_url,
        _reset_public_schema,
    )

    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    previous_key = admin_auth.build_source_rate_limit_keys("203.0.113.88", settings)[1]
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)

    with _connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            ) VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, now, now),
        )
        conn.commit()
        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now + timedelta(seconds=200),
            window_seconds=60,
            lockout_seconds=60,
        )
        assert deleted >= 1
        conn.rollback()


class _AuditSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"id": len(self.calls)}


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    spy = _AuditSpy()
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-candidate",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    assert len(spy.calls) == 1
    event = spy.calls[0]
    assert event["actor_context"].actor == "anonymous"
    assert "attacker-candidate" not in str(event)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_actor_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    spy = _AuditSpy()
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
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
    assert spy.calls[0]["actor_context"].actor == "anonymous"
    assert spy.calls[0]["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in str(spy.calls[0])


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    spy = _AuditSpy()
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=True):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure",
                        side_effect=spy,
                    ):
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": "probe-user",
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert response.status_code == 400
    assert spy.calls[0]["actor_context"].actor == "anonymous"
    assert spy.calls[0]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    spy = _AuditSpy()
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
                ):
                    client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert any(call["reason"] == "rate_limited" for call in spy.calls)
    assert all(call["actor_context"].actor == "anonymous" for call in spy.calls)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    with shared_rate_limiter(FakeRateLimitStore()):
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
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_login_failure_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    candidate = "attacker-supplied-name"
    with caplog.at_level(logging.ERROR):
        with shared_rate_limiter(FakeRateLimitStore()):
            with mock_db_connection():
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure",
                        side_effect=RuntimeError("audit down"),
                    ):
                        client.post(
                            "/admin/login",
                            data={
                                "username": candidate,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_limiter_rows_store_hmac_identifiers() -> None:
    from app.migrations.runner import apply_migrations
    from tests.test_admin_login_rate_limit_integration import (
        _connect,
        _require_database_url,
        _reset_public_schema,
    )

    database_url = _require_database_url()
    settings = get_settings()
    source = "203.0.113.42"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    assert source_key != _plain_sha256("src", source)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)

    with _connect(database_url) as conn:
        db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        conn.commit()
        row = conn.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        ).fetchone()
        assert row is not None
        assert row["limiter_key"] == source_key
        assert len(row["limiter_key"]) == 64
        conn.rollback()


@pytest.mark.integration
def test_postgres_login_failure_audit_actor_is_anonymous() -> None:
    from app.migrations.runner import apply_migrations
    from app.repositories.postgres import PostgresAuditEventRepository
    from tests.test_admin_login_rate_limit_integration import (
        _require_database_url,
        _reset_public_schema,
    )

    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)

    repo = PostgresAuditEventRepository()
    conn = psycopg.connect(database_url, row_factory=psycopg.rows.dict_row, autocommit=False)
    try:
        audit_service.record_login_failure(
            conn,
            actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg-1"),
            reason="invalid_credentials",
            repository=repo,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        ).fetchone()
        assert row is not None
        assert row["actor"] == "anonymous"
        assert row["summary_after"] == {"reason": "invalid_credentials"}
        assert "operator" not in str(row)
    finally:
        conn.close()
