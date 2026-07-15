"""Tests for HMAC admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import LoginAdmissionResult
from app.actor_context import ActorContext
from app.admin_security import validate_admin_security_config, validate_admin_secret_value
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET, TEST_LIMITER_SECRET_PREVIOUS

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
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
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SESSION_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_secret_previous": "",
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256_limiter_key("src", source.lower())
    assert account_key != _plain_sha256_limiter_key("acct", account.lower())
    assert len(source_key) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", source_key)


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    material = "203.0.113.10"
    current = admin_auth.build_source_rate_limit_key(
        material,
        _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET),
    )
    rotated = admin_auth.build_source_rate_limit_key(
        material,
        _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS),
    )
    assert current != rotated


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_secret_and_input() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    payload = "203.0.113.10"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
def test_limiter_secret_validation_rejects_missing_weak_and_placeholder() -> None:
    with pytest.raises(ValueError, match="required"):
        validate_admin_secret_value("", env_name="ADMIN_LOGIN_LIMITER_SECRET")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        validate_admin_secret_value("short-secret", env_name="ADMIN_LOGIN_LIMITER_SECRET")
    with pytest.raises(ValueError, match="placeholder"):
        validate_admin_secret_value(
            "changeme-login-limiter-secret-32bytes!!",
            env_name="ADMIN_LOGIN_LIMITER_SECRET",
        )


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    assert not settings.admin_auth_configured


@pytest.mark.unit
def test_validate_admin_security_config_accepts_strong_secret() -> None:
    validate_admin_security_config(_settings())


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys() -> None:
    settings = _settings(admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    keys = admin_auth.build_source_rate_limit_keys("203.0.113.10", settings)
    assert len(keys) == 2
    assert keys[0] != keys[1]


@pytest.mark.unit
def test_rotation_previous_key_matches_legacy_rows() -> None:
    settings = _settings(admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    previous_only = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )
    rotated = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    legacy = admin_auth.build_source_rate_limit_key("203.0.113.10", previous_only)
    assert legacy in admin_auth.build_source_rate_limit_keys("203.0.113.10", settings)
    assert rotated != legacy


@contextmanager
def _mock_login_flow() -> Generator[None, None, None]:
    with (
        patch("app.admin_routes._try_claim_login_flow", return_value=True),
        patch("app.admin_routes.db.create_admin_session", return_value=42),
        patch("app.admin_routes.db.db_connection") as db_conn,
    ):
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield


@pytest.mark.unit
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    with _mock_login_flow():
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit:
            with patch.object(
                audit_service,
                "get_repositories",
                return_value=MagicMock(audit_events=repo),
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
                repo.append.assert_called_once()
                append_kwargs = repo.append.call_args.kwargs
                assert append_kwargs["actor"] == "anonymous"
                assert "attacker-candidate" not in json.dumps(append_kwargs)


@pytest.mark.unit
def test_configured_username_wrong_password_actor_remains_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}
    with _mock_login_flow():
        with patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit:
            with patch.object(
                audit_service,
                "get_repositories",
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
                actor_context = failure_audit.call_args.kwargs["actor_context"]
                assert actor_context.actor == "anonymous"
                assert TEST_USERNAME not in json.dumps(repo.append.call_args.kwargs)


@pytest.mark.unit
def test_invalid_flow_audit_actor_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-3"}
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    with patch("app.admin_routes._try_claim_login_flow", return_value=False):
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=LoginAdmissionResult(
                admitted=True,
                throttled=False,
                already_locked=False,
                lockout_transition=False,
            ),
        ):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch.object(
                    audit_service,
                    "get_repositories",
                    return_value=MagicMock(audit_events=repo),
                ):
                    with patch("app.admin_routes.db.db_connection") as db_conn:
                        conn = MagicMock()
                        db_conn.return_value.__enter__.return_value = conn
                        db_conn.return_value.__exit__.return_value = None
                        with patch(
                            "app.admin_routes.db.consume_admin_login_flow",
                            return_value=True,
                        ):
                            response = client.post(
                                "/admin/login",
                                data={
                                    "username": "attacker-candidate",
                                    "password": "wrong-password",
                                    "csrf_token": "flow-csrf",
                                },
                            )
                            assert response.status_code == 400
                            failure_audit.assert_called_once()
                            assert (
                                failure_audit.call_args.kwargs["actor_context"].actor
                                == "anonymous"
                            )
                            assert (
                                failure_audit.call_args.kwargs["reason"] == "invalid_csrf"
                            )


@pytest.mark.unit
def test_successful_login_retains_authenticated_actor() -> None:
    with _mock_login_flow():
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


@pytest.mark.unit
def test_failed_login_logs_do_not_leak_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with _mock_login_flow():
        response = client.post(
            "/admin/login",
            data={
                "username": "attacker-candidate",
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
        assert response.status_code == 401
    combined = caplog.text
    assert "attacker-candidate" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113." not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_failure_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source = "203.0.113.88"
    expected_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain_key = _plain_sha256_limiter_key("src", source.lower())
    assert expected_key != plain_key

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        apply_migrations(conn)

        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(expected_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (expected_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["limiter_key"] == expected_key
        assert row["limiter_key"] != plain_key

        repo = PostgresAuditEventRepository()
        with crm_transaction(conn):
            audit_service.record_login_failure(
                conn,
                actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg-1"),
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
        payload = json.dumps(
            {
                "summary_after": audit_row["summary_after"],
                "metadata": audit_row["metadata"],
            }
        )
        assert TEST_USERNAME not in payload
        assert "attacker" not in payload

        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.commit()


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_secret_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    previous_settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )
    previous_key = admin_auth.build_source_rate_limit_key("203.0.113.77", previous_settings)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        apply_migrations(conn)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        db.try_admit_admin_login(
            conn,
            limiter_keys=(previous_key,),
            now=now,
            rate_limit=5,
            window_seconds=60,
            lockout_seconds=60,
        )

        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now + timedelta(seconds=200),
            window_seconds=60,
            lockout_seconds=60,
        )
        assert deleted >= 1

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
            count_row = cur.fetchone()
        assert count_row is not None
        assert int(count_row["count"]) == 0

        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.commit()
