"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    mock_db_connection,
    rate_limit_store,
    shared_rate_limiter,
    _login,
)

client = TestClient(app, follow_redirects=False)
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
PREVIOUS_LIMITER_SECRET = "previous-login-limiter-secret-32chars-min!!"
ALT_LIMITER_SECRET = "alternate-login-limiter-secret-32chars-min!!"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    return Settings(
        database_url=overrides.get("database_url", "postgresql://test:test@localhost:5432/test"),
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url=overrides.get("base_url", "http://testserver"),
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=overrides.get("admin_username", TEST_USERNAME),
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=overrides.get(
            "admin_login_limiter_secret", TEST_LIMITER_SECRET
        ),
        admin_login_limiter_secret_previous=overrides.get(
            "admin_login_limiter_secret_previous", ""
        ),
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    plain = hashlib.sha256(b"src:203.0.113.1").hexdigest()
    assert key != plain
    assert len(key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    left = admin_auth.build_source_rate_limit_key(
        "203.0.113.1",
        _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET),
    )
    right = admin_auth.build_source_rate_limit_key(
        "203.0.113.1",
        _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET),
    )
    assert left != right


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("203.0.113.1", settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_values(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secret_value(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_configuration_validation_requires_current_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_login_limiter_configuration(settings)


@pytest.mark.unit
def test_limiter_rotation_uses_previous_secret_for_lockout_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    current_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    legacy_keys = admin_auth.legacy_login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.88",
    )
    assert len(legacy_keys) == 2
    assert current_key not in legacy_keys


@pytest.mark.unit
def test_legacy_lockout_blocks_admission_without_incrementing_current_key(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    legacy_source = admin_auth.legacy_login_limiter_keys(
        settings=settings,
        submitted_username="ghost",
        client_source="testclient",
    )[0]
    now = datetime.now(timezone.utc)
    rate_limit_store.rows[legacy_source] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }
    scope = {
        "type": "http",
        "headers": [],
        "client": ("testclient", 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username="ghost",
            )

    assert not admission.admitted
    assert admission.already_locked
    current_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert current_key not in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor() -> None:
    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-1"}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch(
                    "app.audit_service.get_repositories",
                    return_value=MagicMock(audit_events=audit_repo),
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
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    append_kwargs = audit_repo.append.call_args.kwargs
    payload = json.dumps(
        {
            "actor": append_kwargs.get("actor"),
            "metadata": append_kwargs.get("metadata"),
            "summary_after": append_kwargs.get("summary_after"),
        }
    )
    assert "attacker-candidate" not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
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
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_keeps_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "csrf-candidate",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )

    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
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
def test_failed_login_logs_exclude_candidate_and_secret(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    candidate = "log-candidate-user"
    with shared_rate_limiter(rate_limit_store):
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
    combined = "\n".join(record.getMessage() for record in caplog.records)
    combined += str(getattr(caplog, "text", ""))
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@contextmanager
def _pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.200", settings)
    plain = hashlib.sha256(b"src:203.0.113.200").hexdigest()
    assert source_key != plain

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)

    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with _pg_conn(database_url) as conn:
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (source_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["limiter_key"] == source_key
        assert len(row["limiter_key"]) == 64


@pytest.mark.integration
def test_postgres_login_failure_audit_actor_is_anonymous() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "pg-candidate",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )

    assert response.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_secret_rows() -> None:
    database_url = _require_database_url()
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    legacy_key = admin_auth.legacy_login_limiter_keys(
        settings=settings,
        submitted_username="ghost",
        client_source="203.0.113.201",
    )[0]

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)

    stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with _pg_conn(database_url) as conn:
        db.try_admit_admin_login(
            conn,
            limiter_keys=(legacy_key,),
            now=stale,
            rate_limit=5,
            window_seconds=60,
            lockout_seconds=60,
        )
        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=stale + timedelta(seconds=200),
            window_seconds=60,
            lockout_seconds=60,
        )
        assert deleted >= 1
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
            row = cur.fetchone()
        assert row is not None
        assert int(row["count"]) == 0
