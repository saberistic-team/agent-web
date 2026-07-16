"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import AdminSecurityConfigError, validate_admin_security_config
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LOGIN_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET_B = "alternate-login-limiter-secret-32chars!!"
TEST_SOURCE = "203.0.113.50"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LOGIN_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    import os

    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LOGIN_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
    }
    env.update(overrides)
    for key, value in env.items():
        os.environ[key] = value
    for key in (
        "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "ADMIN_TRUSTED_EDGE_CIDRS",
    ):
        if key not in overrides:
            os.environ.pop(key, None)
    return get_settings()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_conn,
        patch("app.admin_auth.db.db_connection") as auth_conn,
    ):
        route_conn.return_value.__enter__.return_value = conn
        route_conn.return_value.__exit__.return_value = None
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        yield conn


class AuditRepositorySpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": len(self.events), **kwargs}


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    material = "203.0.113.10"
    plain = hashlib.sha256(f"src:{material}".encode("utf-8")).hexdigest()
    keyed = admin_auth.build_source_rate_limit_key(material, settings)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LOGIN_LIMITER_SECRET)
    settings_b = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_B)
    key_a = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_USERNAME, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", ""),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "changeme"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "placeholder-secret-value-here"),
    ],
)
def test_admin_security_secret_validation_rejects_weak_limiter_values(
    field: str, value: str
) -> None:
    settings = _settings(**{field: value})
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_admin_security_session_secret_validation_requires_auth_context() -> None:
    settings = _settings(ADMIN_SESSION_SECRET="")
    assert not settings.admin_auth_configured
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_previous_secret_produces_distinct_check_keys() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_B,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LOGIN_LIMITER_SECRET,
    )
    write_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    check_keys = admin_auth.login_limiter_check_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(write_keys) == 2
    assert len(check_keys) == 4
    assert set(write_keys).issubset(set(check_keys))


@pytest.mark.unit
def test_rotation_honors_previous_key_lockout_without_incrementing_it() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_B,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LOGIN_LIMITER_SECRET,
    )
    previous_source = admin_auth.build_source_rate_limit_key(
        TEST_SOURCE,
        _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LOGIN_LIMITER_SECRET),
    )
    write_source = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchall.side_effect = [
        [
            {
                "limiter_key": previous_source,
                "failure_count": 5,
                "window_started_at": now,
                "locked_until": now.replace(year=2027),
            }
        ],
        [],
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=(previous_source, write_source),
        increment_limiter_keys=(write_source,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.unit
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as record_failure:
            with mock_db_connection():
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost-attacker",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    record_failure.assert_called_once()
    actor_context = record_failure.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert record_failure.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as record_failure:
            with mock_db_connection():
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    assert record_failure.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
def test_invalid_csrf_failure_audit_is_anonymous_without_candidate() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=False):
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as record_failure:
            with mock_db_connection():
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "attacker@example.com",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 400
    record_failure.assert_called_once()
    kwargs = record_failure.call_args.kwargs
    assert kwargs["actor_context"].actor == "anonymous"
    assert kwargs["reason"] == "invalid_csrf"
    assert kwargs["actor_context"].actor != "attacker@example.com"


@pytest.mark.unit
def test_successful_login_retains_authenticated_actor() -> None:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.create_admin_session", return_value=42):
            with patch(
                "app.admin_routes.audit_service.record_login_success"
            ) as success_audit:
                with mock_db_connection():
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
def test_failed_login_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with mock_db_connection():
            response = client.post(
                "/admin/login",
                data={
                    "username": "attacker@example.com",
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert response.status_code == 401
    combined = caplog.text
    for forbidden in (
        "attacker@example.com",
        TEST_LOGIN_LIMITER_SECRET,
        TEST_SECRET,
        "src:203.0.113",
        "acct:operator",
    ):
        assert forbidden not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_and_anonymous_failure_actor() -> None:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    if not database_url:
        if required:
            pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
        pytest.skip("TEST_DATABASE_URL not set")

    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    plain = hashlib.sha256(f"src:{TEST_SOURCE}".encode("utf-8")).hexdigest()
    assert source_key != plain

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    repo = PostgresAuditEventRepository()
    try:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (source_key,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["limiter_key"] == source_key
            assert row["limiter_key"] != plain

            with crm_transaction(conn):
                from app.actor_context import ActorContext

                audit_service.record_login_failure(
                    conn,
                    actor_context=ActorContext(actor="anonymous", correlation_id="corr-1"),
                    reason="invalid_credentials",
                    repository=repo,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT actor, summary_after, metadata
                    FROM audit_events
                    WHERE action = 'auth.login.failure'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                audit_row = cur.fetchone()
            assert audit_row is not None
            assert audit_row["actor"] == "anonymous"
            summary = audit_row["summary_after"] or {}
            metadata = audit_row["metadata"] or {}
            assert TEST_USERNAME not in str(summary)
            assert TEST_USERNAME not in str(metadata)
    finally:
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.commit()
