"""Tests for HMAC login limiter identifiers and anonymous failure audit actors."""

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
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum!!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings_with_secret(secret: str, *, previous: str = "") -> Settings:
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
        admin_login_limiter_secret=secret,
        admin_login_limiter_secret_previous=previous,
    )


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter identifier tests")


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256_digest() -> None:
    settings = get_settings()
    source = "203.0.113.1"
    keyed = admin_auth.build_source_rate_limit_key(source, settings)
    plain = admin_auth.plain_sha256_unkeyed_limiter_digest(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        source.strip().lower(),
    )
    assert keyed != plain
    assert keyed != hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secret(TEST_LIMITER_SECRET)
    settings_b = _settings_with_secret(ALT_LIMITER_SECRET)
    source = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings_with_secret(TEST_LIMITER_SECRET)
    first = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    second = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation_for_identical_payload() -> None:
    settings = get_settings()
    payload = "operator"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-admin-login-limiter-secret-value", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        admin_auth.validate_admin_login_limiter_secret(secret, field_name=field_name)


@pytest.mark.unit
def test_validate_admin_security_secrets_rejects_matching_previous_key() -> None:
    settings = _settings_with_secret(TEST_LIMITER_SECRET, previous=TEST_LIMITER_SECRET)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_keys_differ_from_current_secret() -> None:
    settings = _settings_with_secret(
        TEST_LIMITER_SECRET,
        previous=PREVIOUS_LIMITER_SECRET,
    )
    current = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    previous = admin_auth.rotation_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert previous
    assert current != previous


@pytest.mark.unit
def test_rotation_overlap_blocks_on_previous_secret_lockout() -> None:
    settings = _settings_with_secret(
        TEST_LIMITER_SECRET,
        previous=PREVIOUS_LIMITER_SECRET,
    )
    previous_keys = admin_auth.rotation_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="testclient",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with patch("app.admin_auth.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        with patch(
            "app.admin_auth.db.is_admin_login_throttled",
            side_effect=lambda _conn, *, limiter_key, now: limiter_key == previous_keys[0],
        ):
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
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
            request = Request(scope)
            result = admin_auth.try_admit_login_attempt(request, settings, username=TEST_USERNAME)
            assert not result.admitted
            assert result.throttled
            assert result.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_secret_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    previous_settings = _settings_with_secret(PREVIOUS_LIMITER_SECRET)
    previous_key = admin_auth.build_source_rate_limit_key("203.0.113.88", previous_settings)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_key,),
        now=now,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor_only() -> None:
    from tests.test_admin_auth import mock_db_connection

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
                actor_context = failure_audit.call_args.kwargs["actor_context"]
                assert actor_context.actor == "anonymous"
                assert "attacker-candidate" not in str(failure_audit.call_args)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    from tests.test_admin_auth import mock_db_connection

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
                assert TEST_USERNAME not in str(failure_audit.call_args)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_records_anonymous_actor() -> None:
    from tests.test_admin_auth import mock_db_connection

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
def test_lockout_transition_records_anonymous_actor() -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    with patch.dict("os.environ", {"ADMIN_LOGIN_RATE_LIMIT": "2"}):
                        first = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": "wrong",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert first.status_code == 401
                        lockout = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": "wrong",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert lockout.status_code == 401
                        assert failure_audit.call_count == 2
                        last = failure_audit.call_args_list[-1].kwargs
                        assert last["actor_context"].actor == "anonymous"
                        assert last["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    from tests.test_admin_auth import mock_db_connection

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
@pytest.mark.integration
def test_failed_login_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import mock_db_connection

    candidate = "secret-candidate-user"
    with caplog.at_level(logging.INFO):
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

    combined = " ".join(record.getMessage() for record in caplog.records)
    combined += str(caplog.records)
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.fixture
def pg_conn() -> Any:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        bootstrap.commit()
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier_and_anonymous_actor(
    pg_conn: psycopg.Connection,
) -> None:
    from app.actor_context import ActorContext
    from app.crm_uow import crm_transaction

    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.200", settings)
    plain = admin_auth.plain_sha256_unkeyed_limiter_digest(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.200",
    )
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
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

    captured: dict[str, Any] = {}

    class _AuditSpy:
        def append(self, _conn: Any, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"id": "evt-1"}

    actor = ActorContext(actor="anonymous", correlation_id="corr-pg")
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
            reason="invalid_credentials",
            attempted_username="must-not-persist",
            repository=_AuditSpy(),
        )
    assert captured["actor"] == "anonymous"
    assert "must-not-persist" not in str(captured)

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
    assert "must-not-persist" not in str(audit_row)
