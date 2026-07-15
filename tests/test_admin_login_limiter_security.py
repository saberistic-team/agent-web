"""Security tests for keyed admin login limiter identifiers and anonymous audit actors."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

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

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@pytest.fixture(autouse=True)
def limiter_security_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return get_settings()


from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


@contextmanager
def _pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_schema(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        apply_migrations(conn)


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    _reset_schema(database_url)
    with _pg_conn(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            _reset_schema(database_url)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    plain = admin_auth.plain_sha256_limiter_digest("src", "203.0.113.10")
    assert source_key != plain
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    current = _settings()
    alternate = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_ALT)
    key_a = admin_auth.build_source_rate_limit_key("203.0.113.10", current)
    key_b = admin_auth.build_source_rate_limit_key("203.0.113.10", alternate)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", _settings())
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", _settings())
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("operator", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "value", "message"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", "", "required"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short-secret", "at least 32"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "changeme" + "x" * 24, "placeholder"),
        (
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
            "x" * 12,
            "at least 32",
        ),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
    message: str,
) -> None:
    if env_name == "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS":
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", value)
        settings = _settings()
    else:
        settings = _settings(**{env_name: value})
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_limiter_previous_secret_must_differ_from_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_ALT)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_ALT)
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(get_settings())


@pytest.mark.unit
def test_rotation_guard_keys_include_previous_secret_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = _settings()
    key_set = admin_auth.login_limiter_key_set(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(key_set.admit_keys) == 2
    assert len(key_set.guard_keys) == 4
    previous_only = [
        key
        for key in key_set.guard_keys
        if key not in key_set.admit_keys
    ]
    assert len(previous_only) == 2


@pytest.mark.integration
def test_rotation_honors_previous_key_lockout(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    previous_settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_PREVIOUS,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=None,
    )
    previous_key = admin_auth.build_source_rate_limit_key("203.0.113.88", previous_settings)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (
                previous_key,
                now,
                now,
                now + timedelta(minutes=15),
                now,
            ),
        )
        pg_conn.commit()

    current_settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LIMITER_SECRET_PREVIOUS,
    )
    current_key = admin_auth.build_source_rate_limit_key("203.0.113.88", current_settings)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_key,),
        guard_keys=(current_key, previous_key),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
    stale = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.99",
        secret=admin_auth._limiter_secret_bytes(TEST_LIMITER_SECRET_PREVIOUS),
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (stale, now - timedelta(hours=2), now - timedelta(hours=2)),
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
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-candidate",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"
    assert "attempted_username" not in failure_audit.call_args.kwargs


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
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
def test_invalid_csrf_failure_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "csrf-attacker",
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
def test_rate_limited_failure_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    first = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert first.status_code == 401
                    lockout = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert lockout.status_code == 401
    failure_audit.assert_called()
    assert failure_audit.call_args_list[-1].kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args_list[-1].kwargs["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
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
def test_login_failure_logs_exclude_candidate_and_secret(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "secret-candidate-user"
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    return_value=None,
                ):
                    client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    combined = caplog.text + str(caplog.records)
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.42", settings)
    plain = hashlib.sha256(b"src:203.0.113.42").hexdigest()
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted
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

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-1")
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
        audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    serialized = str(audit_row)
    assert "candidate" not in serialized.lower()
    assert TEST_USERNAME not in serialized
