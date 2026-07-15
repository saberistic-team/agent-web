"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET_PREVIOUS = "previous-login-limiter-secret-32bytes!!"
TEST_SOURCE = "203.0.113.42"


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_secret_previous: str = "",
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
        admin_login_limiter_secret_previous=limiter_secret_previous,
    )


@pytest.fixture
def limiter_settings() -> Settings:
    return _settings()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(limiter_settings: Settings) -> None:
    material = TEST_SOURCE
    keyed = admin_auth.build_source_rate_limit_key(material, settings=limiter_settings)
    plain = admin_auth._plain_sha256_limiter_key("src", material.strip().lower())
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(limiter_settings: Settings) -> None:
    other = _settings(limiter_secret=TEST_LIMITER_SECRET_PREVIOUS)
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=limiter_settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=other)
    assert first != second


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls(limiter_settings: Settings) -> None:
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=limiter_settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=limiter_settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation(limiter_settings: Settings) -> None:
    payload = "operator"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings=limiter_settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings=limiter_settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET", "required"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET", "at least 32 bytes"),
        ("change-me-change-me-change-me-change-me!!", "ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET", "too weak"),
    ],
)
def test_limiter_secret_validation_rejects_bad_material(
    secret: str,
    field: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_login_limiter_secret(secret, field=field)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret() -> None:
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET)
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_rotation_lockout_keys_include_previous_secret_rows(limiter_settings: Settings) -> None:
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    keys = admin_auth.login_limiter_lockout_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    previous_source = admin_auth._digest_limiter_key_with_secret(
        TEST_LIMITER_SECRET_PREVIOUS,
        "src",
        TEST_SOURCE.strip().lower(),
    )
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_admits_only_current_keys(limiter_settings: Settings) -> None:
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    admission = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    lockout = admin_auth.login_limiter_lockout_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(admission) == 2
    assert len(lockout) == 4


@pytest.mark.integration
def test_rotation_previous_lockout_blocks_admission_without_incrementing_current(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    previous_source = admin_auth._digest_limiter_key_with_secret(
        TEST_LIMITER_SECRET_PREVIOUS,
        "src",
        TEST_SOURCE.strip().lower(),
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (previous_source, 5, now, now + timedelta(minutes=15), now),
        )
    pg_conn.commit()

    current_source = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_source,),
        lockout_keys=(current_source, previous_source),
        now=now + timedelta(seconds=30),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_source,),
        )
        row = cur.fetchone()
    assert row is None


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    stale_key = admin_auth._digest_limiter_key_with_secret(
        TEST_LIMITER_SECRET_PREVIOUS,
        "src",
        "198.51.100.99",
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (stale_key, now - timedelta(hours=2), now - timedelta(hours=2)),
        )
    pg_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1


@pytest.fixture
def pg_conn(database_url: str) -> Any:
    from tests.test_admin_login_rate_limit_integration import _connect, _reset_public_schema

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with _connect(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


@pytest.fixture(scope="module")
def database_url() -> str:
    from tests.test_admin_login_rate_limit_integration import _require_database_url

    return _require_database_url()


@pytest.mark.integration
def test_pg_persisted_limiter_rows_use_keyed_identifiers(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    plain = admin_auth._plain_sha256_limiter_key("src", TEST_SOURCE.strip().lower())
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain
    assert len(row["limiter_key"]) == 64


def _spy_record_login_failure(
    captured: list[dict[str, Any]],
):
    original = audit_service.record_login_failure

    def _wrapper(conn, *, actor_context, reason, repository=None):
        captured.append(
            {
                "actor": actor_context.actor,
                "reason": reason,
                "correlation_id": actor_context.correlation_id,
            }
        )
        return original(
            conn,
            actor_context=actor_context,
            reason=reason,
            repository=repository,
        )

    return _wrapper


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor(admin_env: None) -> None:
    captured: list[dict[str, Any]] = []
    with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy_record_login_failure(captured)):
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                db_conn.return_value.__exit__.return_value = None
                with patch("app.admin_routes.crm_transaction"):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "ghost-attacker",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    assert captured
    event = captured[-1]
    assert event["actor"] == "anonymous"
    assert "ghost-attacker" not in str(event)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_is_anonymous(admin_env: None) -> None:
    captured: list[dict[str, Any]] = []
    with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy_record_login_failure(captured)):
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                db_conn.return_value.__exit__.return_value = None
                with patch("app.admin_routes.crm_transaction"):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    assert captured[-1]["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_actor_is_anonymous(admin_env: None) -> None:
    captured: list[dict[str, Any]] = []
    with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy_record_login_failure(captured)):
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                with patch("app.admin_routes.db.db_connection") as db_conn:
                    db_conn.return_value.__enter__.return_value = MagicMock()
                    db_conn.return_value.__exit__.return_value = None
                    with patch("app.admin_routes.crm_transaction"):
                        with patch("app.admin_routes._issue_login_flow_response") as issue_flow:
                            issue_flow.return_value = MagicMock(status_code=400)
                            client.post(
                                "/admin/login",
                                data={
                                    "username": "candidate-user",
                                    "password": "wrong-password",
                                    "csrf_token": "flow-csrf",
                                },
                            )
    assert captured
    assert captured[-1]["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(admin_env: None) -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.create_admin_session", return_value=42):
            with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
                with patch("app.admin_routes.db.db_connection") as db_conn:
                    db_conn.return_value.__enter__.return_value = MagicMock()
                    db_conn.return_value.__exit__.return_value = None
                    with patch("app.admin_routes.crm_transaction"):
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


@pytest.mark.unit
def test_failed_login_logs_exclude_candidate_and_secret(
    admin_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    candidate = "attacker-supplied-name"
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.audit_service.record_login_failure", side_effect=RuntimeError("audit down")):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                db_conn.return_value.__enter__.return_value = MagicMock()
                db_conn.return_value.__exit__.return_value = None
                with patch("app.admin_routes.crm_transaction"):
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
    assert TEST_SOURCE not in combined or "203.0.113" not in combined


@pytest.mark.integration
def test_pg_audit_login_failure_actor_is_anonymous(pg_conn: psycopg.Connection, admin_env: None) -> None:
    from app.repositories.postgres import PostgresAuditEventRepository

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-test")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert row["summary_after"]["reason"] == "invalid_credentials"
    assert "username" not in str(row["summary_after"]).lower()
    assert "username" not in str(row["metadata"]).lower()
