"""Security tests for keyed login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository

from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alternate-limiter-secret-32chars-min!"
CANDIDATE_USERNAME = "attacker-candidate@evil.example"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_limiter_digest(domain: str, material: str) -> str:
    payload = f"{domain}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings_with_limiter_secret(
    secret: str,
    *,
    previous: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=secret,
        admin_login_limiter_secret_previous=previous,
    )


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
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_limiter_digest("src", "203.0.113.50")
    assert account_key != _plain_sha256_limiter_digest("acct", "operator")
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_limiter_secret(TEST_LIMITER_SECRET)
    settings_b = _settings_with_limiter_secret(ALT_LIMITER_SECRET)
    material = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.50", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = get_settings()
    shared_material = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        (" secret-padding-to-thirty-two-chars!", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_values(secret: str, env_name: str) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_limiter_secret_value(secret, env_name=env_name)


@pytest.mark.unit
def test_admin_auth_configured_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    assert settings.admin_auth_configured is False


@pytest.mark.unit
def test_validate_admin_security_config_rejects_weak_previous_secret() -> None:
    settings = _settings_with_limiter_secret(TEST_LIMITER_SECRET, previous="short")
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", ALT_LIMITER_SECRET)
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(keys) == 4
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    previous_source = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.10",
        ALT_LIMITER_SECRET,
    )
    assert current_source in keys
    assert previous_source in keys


@pytest.mark.unit
def test_rotation_previous_key_row_still_throttles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", ALT_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    previous_source = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "testclient",
        TEST_LIMITER_SECRET,
    )
    store = FakeRateLimitStore()
    now = datetime.now(timezone.utc)
    store.rows[previous_source] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }
    with shared_rate_limiter(store):
        from starlette.requests import Request

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
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        }
        request = Request(scope)
        throttled = admin_auth.is_login_throttled(
            request,
            settings,
            username=TEST_USERNAME,
        )
    assert throttled is True


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "bad-csrf",
                        },
                    )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success",
                        wraps=audit_service.record_login_success,
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

    caplog.set_level(logging.DEBUG, logger="app")
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": CANDIDATE_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    combined = caplog.text
    assert CANDIDATE_USERNAME not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    stored = str(row["limiter_key"])
    assert stored == source_key
    assert stored != _plain_sha256_limiter_digest("src", "203.0.113.88")
    assert len(stored) == 64


@pytest.mark.integration
def test_postgres_login_failure_audit_row_is_anonymous(pg_conn: psycopg.Connection) -> None:
    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-242")
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
            reason="invalid_credentials",
            repository=repo,
        )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, metadata, summary_after
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
    metadata = row["metadata"]
    assert metadata == {"reason": "invalid_credentials"}
    assert CANDIDATE_USERNAME not in json.dumps(row, default=str)


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    settings = _settings_with_limiter_secret(
        ALT_LIMITER_SECRET,
        previous=TEST_LIMITER_SECRET,
    )
    previous_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.99",
        TEST_LIMITER_SECRET,
    )
    stale_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, NULL, %s)
            """,
            (previous_key, 1, stale_time, stale_time),
        )
    pg_conn.commit()
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    current_key = admin_auth.build_source_rate_limit_key("203.0.113.99", settings)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    removed = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()
    assert removed >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0
