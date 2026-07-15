"""Tests for keyed admin login limiter identifiers and anonymous failure actors (#242)."""

from __future__ import annotations

import hashlib
import json
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
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min-x"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    return Settings(
        database_url=overrides.get("database_url", base.database_url),
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=overrides.get("admin_username", base.admin_username),
        admin_password_hash=overrides.get("admin_password_hash", base.admin_password_hash),
        admin_session_secret=overrides.get("admin_session_secret", base.admin_session_secret),
        admin_login_limiter_secret=overrides.get(
            "admin_login_limiter_secret", base.admin_login_limiter_secret
        ),
        admin_login_limiter_secret_previous=overrides.get(
            "admin_login_limiter_secret_previous",
            base.admin_login_limiter_secret_previous,
        ),
        admin_session_ttl_seconds=base.admin_session_ttl_seconds,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
        admin_trust_proxy_headers=base.admin_trust_proxy_headers,
        audit_page_size=base.audit_page_size,
        brief_page_size=base.brief_page_size,
    )


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    account = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256_limiter_key("src", source)
    assert account_key != _plain_sha256_limiter_key("acct", account)
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(
        admin_login_limiter_secret="another-limiter-secret-32chars-min-y"
    )
    source = "203.0.113.1"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    source = "203.0.113.99"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_separates_source_and_account_domains() -> None:
    settings = _settings()
    material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(material, settings)
    account_key = admin_auth.build_account_rate_limit_key(material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str, env_name: str
) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_validate_admin_security_secrets_checks_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", "weak")
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_lookup_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
        include_previous=True,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    previous_source = admin_auth._hmac_limiter_digest(
        "src", "203.0.113.1", TEST_PREVIOUS_LIMITER_SECRET
    )
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_cleanup_removes_stale_previous_key_rows() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = 1

    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    sql = cur.execute.call_args_list[0].args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    captured: dict[str, Any] = {}

    def _capture(
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> None:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        captured["repository"] = repository

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
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
                assert captured["actor"] == "anonymous"
                assert captured["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
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
                actor_context = failure_audit.call_args.kwargs["actor_context"]
                assert actor_context.actor == "anonymous"
                repo.append.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.audit_service.record_login_failure"
            ) as failure_audit:
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "attacker-candidate",
                        "password": TEST_PASSWORD,
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 400
                failure_audit.assert_called_once()
                assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
                assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.db.create_admin_session", return_value=42
            ):
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


@pytest.mark.unit
def test_login_failure_logs_do_not_contain_candidate_username(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "attacker-candidate@example.com"
    with caplog.at_level(logging.ERROR):
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
    blob = caplog.text + str(caplog.records)
    assert candidate not in blob
    assert TEST_LIMITER_SECRET not in blob
    assert "src:" not in blob
    assert "acct:" not in blob


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter audit tests")


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
def pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    plain = _plain_sha256_limiter_key("src", "203.0.113.88")
    assert source_key != plain

    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
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

    candidate = "attacker-candidate@example.com"
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=MagicMock(actor="anonymous", correlation_id="corr-242"),
            reason="invalid_credentials",
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
    payload = json.dumps(
        {
            "summary_after": audit_row["summary_after"],
            "metadata": audit_row["metadata"],
        }
    )
    assert candidate not in payload
    assert TEST_USERNAME not in payload
