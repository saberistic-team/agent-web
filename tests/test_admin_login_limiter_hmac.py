"""Tests for HMAC login limiter identifiers and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
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
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
    _fetch_login_form,
    _login,
)

client = TestClient(app, follow_redirects=False)

_ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
_PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum"


def _settings(**overrides: str) -> Settings:
    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
        **overrides,
    }
    for key, value in env.items():
        os.environ[key] = value
    return get_settings()


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    return hashlib.sha256(f"{prefix}:{material}".encode("utf-8")).hexdigest()


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
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_limiter_key("src", "203.0.113.1")
    assert account_key != _plain_sha256_limiter_key("acct", "operator")
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    current = _settings()
    alternate = _settings(ADMIN_LOGIN_LIMITER_SECRET=_ALT_LIMITER_SECRET)
    source_current = admin_auth.build_source_rate_limit_key("203.0.113.1", current)
    source_alt = admin_auth.build_source_rate_limit_key("203.0.113.1", alternate)
    assert source_current != source_alt


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth._digest_limiter_key(
        "src", shared_material, settings.admin_login_limiter_secret
    )
    account_key = admin_auth._digest_limiter_key(
        "acct", shared_material, settings.admin_login_limiter_secret
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("a" * 32, "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(admin_auth.AdminLoginLimiterSecretError):
        admin_auth.validate_admin_login_security(settings)


@pytest.mark.unit
def test_rotation_previous_secret_blocks_admission_for_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", _ALT_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", _PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    legacy_keys = admin_auth._login_limiter_keys_for_secret(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.9",
        configured_admin_username=TEST_USERNAME,
        secret=_PREVIOUS_LIMITER_SECRET,
    )
    now = datetime.now(timezone.utc)
    store = FakeRateLimitStore()
    for key in legacy_keys:
        store.rows[key] = {
            "failure_count": 5,
            "window_started_at": now,
            "locked_until": now + timedelta(seconds=900),
            "updated_at": now,
        }

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
        "client": ("203.0.113.9", 12345),
        "server": ("testserver", 80),
    }
    from starlette.requests import Request

    request = Request(scope)
    with shared_rate_limiter(store):
        result = admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=TEST_USERNAME,
        )
    assert not result.admitted
    assert result.throttled


@pytest.mark.unit
def test_rotation_cleanup_removes_previous_secret_rows() -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store: dict[str, dict[str, Any]] = {
        "legacy-key": {
            "failure_count": 1,
            "window_started_at": now - timedelta(seconds=400),
            "locked_until": None,
            "updated_at": now - timedelta(seconds=400),
        }
    }

    def cleanup(
        conn: Any,
        *,
        now: datetime,
        window_seconds: int,
        lockout_seconds: int,
    ) -> int:
        retention = max(window_seconds, lockout_seconds) * 2
        cutoff = now - timedelta(seconds=retention)
        expired = [
            key
            for key, row in store.items()
            if row["updated_at"] < cutoff
            and (row["locked_until"] is None or row["locked_until"] < now)
        ]
        for key in expired:
            del store[key]
        return len(expired)

    deleted = cleanup(
        MagicMock(),
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    assert "legacy-key" not in store


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch(
                    "app.audit_service.get_repositories",
                    return_value=MagicMock(audit_events=repo),
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "ghost-attacker",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    failure_audit.assert_called_once()
                    actor = failure_audit.call_args.kwargs["actor_context"].actor
                    assert actor == "anonymous"
                    append_actor = repo.append.call_args.kwargs["actor"]
                    assert append_actor == "anonymous"
                    metadata = repo.append.call_args.kwargs.get("metadata") or {}
                    summary = repo.append.call_args.kwargs.get("summary_after") or {}
                    assert "ghost-attacker" not in str(metadata)
                    assert "ghost-attacker" not in str(summary)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch(
                    "app.audit_service.get_repositories",
                    return_value=MagicMock(audit_events=repo),
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
                    failure_audit.assert_called_once()
                    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"
                    assert TEST_USERNAME not in str(repo.append.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_actor_is_anonymous() -> None:
    from tests.test_admin_auth import FakeRateLimitStore

    store = FakeRateLimitStore()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-3"}
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch(
                        "app.audit_service.get_repositories",
                        return_value=MagicMock(audit_events=repo),
                    ):
                        csrf_token, cookies = _fetch_login_form()
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "tampered-token",
                            },
                            cookies=cookies,
                        )
                        assert response.status_code == 400
                        failure_audit.assert_called_once()
                        assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"
                        assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    login = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert login.status_code == 303
                    success_audit.assert_called_once()
                    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": "leak-candidate-user",
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
            assert response.status_code == 401
    combined = caplog.text + str(response.text)
    assert "leak-candidate-user" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:leak" not in combined


_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
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


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    assert source_key != _plain_sha256_limiter_key("src", "203.0.113.88")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
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
    assert len(row["limiter_key"]) == 64

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg")
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
    assert "ghost" not in str(audit_row["summary_after"])
    assert "ghost" not in str(audit_row["metadata"])
