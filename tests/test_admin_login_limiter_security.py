"""Security tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.repositories.postgres import PostgresAuditEventRepository

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-login-limiter-key-32bytes-minimum!"
TEST_PREVIOUS_LIMITER_SECRET = "previous-login-limiter-key-32bytes-min!"
TEST_SOURCE = "203.0.113.42"
TEST_CANDIDATE = "ghost-attacker"


@pytest.fixture(autouse=True)
def limiter_security_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _settings() -> Settings:
    return get_settings()


def _plain_sha256_identifier(domain: str, material: str) -> str:
    payload = f"{domain}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings=settings)

    assert source_key != _plain_sha256_identifier("src", TEST_SOURCE.lower())
    assert account_key != _plain_sha256_identifier("acct", TEST_USERNAME.lower())
    assert len(source_key) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", source_key)


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    material = TEST_SOURCE.lower()
    current = admin_auth.digest_limiter_key(
        TEST_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        material,
    )
    alternate = admin_auth.digest_limiter_key(
        "alternate-login-limiter-key-32bytes-min!",
        admin_auth.LIMITER_DOMAIN_SOURCE,
        material,
    )
    assert current != alternate


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domains_are_separated() -> None:
    settings = _settings()
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET", "required"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET", "32 bytes"),
        ("placeholder-limiter-key-32bytes-minimum!", "ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_startup_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_LIMITER_SECRET)
    settings = _settings()
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_read_keys_include_previous_secret_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_PREVIOUS_LIMITER_SECRET)
    settings = _settings()
    write_keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
    )
    read_keys = admin_auth.login_limiter_read_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
    )
    assert len(write_keys) == 2
    assert len(read_keys) == 4
    assert set(write_keys).issubset(read_keys)


@pytest.mark.integration
def test_rotation_honors_previous_secret_lockout(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    previous_source_key = admin_auth.digest_limiter_key(
        TEST_PREVIOUS_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        TEST_SOURCE.lower(),
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                previous_source_key,
                5,
                now,
                now + timedelta(seconds=900),
                now,
            ),
        )
        pg_conn.commit()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_PREVIOUS_LIMITER_SECRET)
    settings = _settings()
    write_keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
    )
    read_keys = admin_auth.login_limiter_read_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
    )
    assert admin_auth._any_limiter_locked(pg_conn, limiter_keys=read_keys, now=now)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write_keys,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    monkeypatch.undo()


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_secret_rows(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    stale_key = admin_auth.digest_limiter_key(
        TEST_PREVIOUS_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "198.51.100.9",
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (stale_key, 1, now - timedelta(hours=2), None, now - timedelta(hours=2)),
        )
        pg_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1


class _AuditSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"id": str(len(self.events))}


@contextmanager
def _login_flow_mocks() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1}),
        patch("app.admin_routes.db.create_admin_session", return_value=42),
        patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=None),
        patch("app.admin_routes.db.revoke_admin_session", return_value=True),
        patch("app.admin_auth.db.db_connection") as auth_db_conn,
        patch("app.admin_auth.db.try_admit_admin_login") as try_admit,
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
    ):
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        auth_db_conn.return_value.__enter__.return_value = conn
        auth_db_conn.return_value.__exit__.return_value = None
        try_admit.return_value = db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )
        yield conn


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    with _login_flow_mocks():
        response = client.get("/admin/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    cookies = {
        admin_auth.LOGIN_FLOW_COOKIE_NAME: response.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]
    }
    return match.group(1), cookies


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor_only() -> None:
    spy = _AuditSpy()
    with _login_flow_mocks():
        with patch("app.admin_routes.audit_service.record_login_failure", wraps=audit_service.record_login_failure) as wrapped:
            with patch("app.admin_routes.audit_service.record_event") as record_event:
                record_event.side_effect = lambda conn, **kwargs: spy.append(conn, **kwargs) or {"id": "1"}
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401
                wrapped.assert_called_once()
                assert wrapped.call_args.kwargs["actor_context"].actor == "anonymous"
                assert wrapped.call_args.kwargs["reason"] == "invalid_credentials"
                assert TEST_CANDIDATE not in str(wrapped.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    spy = _AuditSpy()
    with _login_flow_mocks():
        with patch("app.admin_routes.audit_service.record_event") as record_event:
            record_event.side_effect = lambda conn, **kwargs: spy.append(conn, **kwargs) or {"id": "1"}
            csrf_token, cookies = _fetch_login_form()
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            assert response.status_code == 401
            assert len(spy.events) == 1
            event = spy.events[0]
            assert event["actor_context"].actor == "anonymous"
            assert event["summary_after"] == {"reason": "invalid_credentials"}
            assert TEST_USERNAME not in str(event)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor() -> None:
    spy = _AuditSpy()
    with _login_flow_mocks():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes.audit_service.record_event") as record_event:
                record_event.side_effect = lambda conn, **kwargs: spy.append(conn, **kwargs) or {"id": "1"}
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 400
                assert len(spy.events) == 1
                assert spy.events[0]["actor_context"].actor == "anonymous"
                assert spy.events[0]["summary_after"]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    spy = _AuditSpy()
    with _login_flow_mocks():
        with patch("app.admin_auth.db.try_admit_admin_login") as try_admit:
            try_admit.side_effect = [
                db.AdminLoginAdmission(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=False,
                ),
                db.AdminLoginAdmission(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=True,
                ),
            ]
            with patch("app.admin_routes.audit_service.record_event") as record_event:
                record_event.side_effect = lambda conn, **kwargs: spy.append(conn, **kwargs) or {"id": "1"}
                csrf_token, cookies = _fetch_login_form()
                first = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert first.status_code == 401
                csrf_token, cookies = _fetch_login_form()
                second = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert second.status_code == 401
                assert len(spy.events) == 2
                assert spy.events[-1]["actor_context"].actor == "anonymous"
                assert spy.events[-1]["summary_after"]["reason"] == "rate_limited"
                assert TEST_CANDIDATE not in str(spy.events[-1])


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with _login_flow_mocks():
        with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
            csrf_token, cookies = _fetch_login_form()
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            assert response.status_code == 303
            success_audit.assert_called_once()
            assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
            assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_exclude_candidate_and_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with _login_flow_mocks():
        csrf_token, cookies = _fetch_login_form()
        with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
            failure_audit.side_effect = RuntimeError("audit store failed")
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_CANDIDATE,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            assert response.status_code == 401

    combined = caplog.text
    assert TEST_CANDIDATE not in combined
    assert TEST_SOURCE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier_and_anonymous_actor(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    repo = PostgresAuditEventRepository()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings=settings)
    plain_key = _plain_sha256_identifier("src", TEST_SOURCE.lower())
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

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
    assert row["limiter_key"] != plain_key

    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-1")
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
    assert audit_row["summary_after"] == {"reason": "invalid_credentials"}
    assert TEST_CANDIDATE not in str(audit_row)


# Reuse Postgres fixtures from the login limiter integration suite.
@pytest.fixture
def pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    from tests.test_admin_login_rate_limit_integration import (  # noqa: PLC0415
        _connect,
        _reset_public_schema,
    )
    from app.migrations.runner import apply_migrations

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
