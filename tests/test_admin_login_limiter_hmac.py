"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_security import (
    AdminSecurityConfigError,
    validate_admin_login_limiter_secret,
    validate_admin_security_config,
)
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET, TEST_LIMITER_SECRET_PREVIOUS

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

client = TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
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
    with patch.dict("os.environ", env, clear=False):
        return get_settings()


from tests.test_admin_auth import mock_db_connection as full_mock_db_connection
from tests.test_admin_auth import shared_rate_limiter, FakeRateLimitStore


def _plain_sha256_identifier(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter/audit tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@pytest.mark.unit
def test_startup_validation_skips_preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "short")
    settings = get_settings()
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_startup_validation_requires_strong_limiter_secret() -> None:
    settings = _settings(ADMIN_LOGIN_LIMITER_SECRET="weak-short-secret-value-here")
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_identifier("src", "203.0.113.10")
    assert account_key != _plain_sha256_identifier("acct", "operator")
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET)
    settings_b = _settings(
        ADMIN_LOGIN_LIMITER_SECRET="alternate-limiter-secret-32chars-min!!"
    )
    material = "203.0.113.10"
    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.10"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    [
        "",
        "short",
        "changeme-please-use-a-real-secret-value",
        "placeholder-limiter-secret-32chars-min",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str) -> None:
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LIMITER_SECRET,
    )
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LIMITER_SECRET_PREVIOUS,
    )
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    previous_source = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_PREVIOUS,
        "src",
        "203.0.113.10",
    )
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_cleanup_still_targets_previous_key_rows() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LIMITER_SECRET_PREVIOUS,
    )
    previous_source = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_PREVIOUS,
        "src",
        "203.0.113.66",
    )
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 1
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    sql = cur.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql
    assert previous_source != admin_auth.build_source_rate_limit_key("203.0.113.66", settings)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-1"}
    with shared_rate_limiter(rate_limit_store):
        with full_mock_db_connection() as conn:
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch("app.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = audit_repo
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": "ghost-candidate",
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    append_kwargs = audit_repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    serialized = json.dumps(append_kwargs)
    assert "ghost-candidate" not in serialized


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-2"}
    with shared_rate_limiter(rate_limit_store):
        with full_mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    wraps=audit_service.record_login_failure,
                ) as failure_audit:
                    with patch("app.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = audit_repo
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert response.status_code == 401
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert audit_repo.append.call_args.kwargs["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with full_mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure"
            ) as failure_audit:
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": "wrong-csrf",
                    },
                )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert "attempted_username" not in failure_audit.call_args.kwargs


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with full_mock_db_connection():
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
    caplog: pytest.LogCaptureFixture,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    candidate = "attacker-candidate@evil.example"
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with full_mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.audit_service.record_login_failure"):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    combined = caplog.text + str(response.content)
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113." not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_and_anonymous_actor(
    database_url: str,
) -> None:
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        apply_migrations(conn)
        settings = _settings()
        source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
        assert source_key != _plain_sha256_identifier("src", "203.0.113.88")
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (source_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert re.fullmatch(r"[0-9a-f]{64}", row["limiter_key"])

        from app.actor_context import ActorContext

        with crm_transaction(conn):
            audit_service.record_login_failure(
                conn,
                actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg"),
                reason="invalid_credentials",
            )
        conn.commit()
        with conn.cursor() as cur:
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
        serialized = json.dumps(
            {
                "summary_after": audit_row["summary_after"],
                "metadata": audit_row["metadata"],
            },
            default=str,
        )
        assert TEST_USERNAME not in serialized
        assert "ghost" not in serialized.lower()
