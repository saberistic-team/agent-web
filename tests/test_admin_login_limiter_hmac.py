"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import validate_admin_login_limiter_secret, validate_admin_security_at_startup
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET, TEST_LIMITER_SECRET_PREVIOUS

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
CANDIDATE_USERNAME = "attacker-candidate@example.com"
CLIENT_SOURCE = "203.0.113.50"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


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
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
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


client = TestClient(app, follow_redirects=False)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    return get_settings()


def _admitted_login_attempt() -> admin_auth.LoginAdmissionResult:
    return admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )


def _plain_sha256_identifier(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(settings: Settings) -> None:
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert source_key != _plain_sha256_identifier("src", CLIENT_SOURCE.lower())
    assert account_key != _plain_sha256_identifier("acct", TEST_USERNAME.lower())
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    first = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_PREVIOUS)
    other_settings = get_settings()
    second = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, other_settings)
    assert first != second


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls(settings: Settings) -> None:
    first = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domains_are_separated(settings: Settings) -> None:
    material = "shared-material"
    source_key = admin_auth._hmac_limiter_digest("src", material, TEST_LIMITER_SECRET)
    account_key = admin_auth._hmac_limiter_digest("acct", material, TEST_LIMITER_SECRET)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short-secret", "must be at least 32 characters"),
        ("changemechangemechangemechangeme", "must not use a placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_startup_validation_requires_strong_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changemechangemechangemechangeme")
    with pytest.raises(ValueError, match="must not use a placeholder"):
        validate_admin_security_at_startup(get_settings())


@pytest.mark.unit
def test_rotation_includes_previous_secret_variants(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    rotated_settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=rotated_settings,
    )
    previous = admin_auth._hmac_limiter_digest(
        "src",
        CLIENT_SOURCE.lower(),
        TEST_LIMITER_SECRET_PREVIOUS,
    )
    assert current in keys
    assert previous in keys
    assert len(keys) == 4


@pytest.mark.integration
def test_rotation_previous_rows_remain_enforceable_and_cleanup(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
    previous_key = admin_auth._hmac_limiter_digest(
        "src",
        CLIENT_SOURCE.lower(),
        TEST_LIMITER_SECRET_PREVIOUS,
    )
    pg_conn.execute(
        """
        INSERT INTO admin_login_rate_limits (
            limiter_key, failure_count, window_started_at, locked_until, updated_at
        )
        VALUES (%s, 5, %s, %s, %s)
        """,
        (
            previous_key,
            now - timedelta(minutes=5),
            now + timedelta(minutes=10),
            now - timedelta(hours=2),
        ),
    )
    pg_conn.commit()

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    keys = admin_auth.login_limiter_keys(
        submitted_username="ghost",
        client_source=CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=keys,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1


class _AuditSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def append(self, _conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"id": "evt-1"}


def _capture_login_failure(spy: _AuditSpy):
    record_failure = audit_service.record_login_failure

    def _record(conn: Any, **kwargs: Any) -> dict[str, Any] | None:
        filtered = {key: value for key, value in kwargs.items() if key != "repository"}
        return record_failure(conn, repository=spy, **filtered)

    return _record


def _mock_audit_db() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    db_conn = MagicMock()
    db_conn.return_value.__enter__.return_value = conn
    db_conn.return_value.__exit__.return_value = None
    tx = MagicMock()
    tx.return_value.__enter__.return_value = None
    tx.return_value.__exit__.return_value = None
    return db_conn, tx


@contextmanager
def _login_flow_ready() -> Generator[None, None, None]:
    with (
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes._try_claim_login_flow", return_value=True),
        patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=_admitted_login_attempt(),
        ),
    ):
        yield


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor_only(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    spy = _AuditSpy()
    db_conn, tx = _mock_audit_db()
    with (
        _login_flow_ready(),
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_routes.crm_transaction", tx),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_capture_login_failure(spy),
        ),
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": CANDIDATE_USERNAME,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
    assert response.status_code == 401
    assert len(spy.calls) == 1
    event = spy.calls[0]
    assert event["actor"] == "anonymous"
    assert CANDIDATE_USERNAME not in json.dumps(event)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    spy = _AuditSpy()
    db_conn, tx = _mock_audit_db()
    with (
        _login_flow_ready(),
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_routes.crm_transaction", tx),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_capture_login_failure(spy),
        ),
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
    assert spy.calls[0]["actor"] == "anonymous"
    assert TEST_USERNAME not in json.dumps(spy.calls[0])


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    spy = _AuditSpy()
    db_conn, tx = _mock_audit_db()
    with (
        patch("app.admin_routes._try_claim_login_flow", return_value=False),
        patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None),
        patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=_admitted_login_attempt(),
        ),
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_routes.crm_transaction", tx),
        patch("app.admin_routes._issue_login_flow_response") as issue_flow,
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_capture_login_failure(spy),
        ),
    ):
        issue_flow.return_value = MagicMock(status_code=400)
        client.post(
            "/admin/login",
            data={
                "username": CANDIDATE_USERNAME,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
    assert spy.calls[0]["actor"] == "anonymous"
    assert spy.calls[0]["summary_after"]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    from app.actor_context import actor_context_from_request

    captured_actor: dict[str, str] = {}

    def _fake_issue_session(
        *,
        request: Request,
        response: Any,
        settings: Settings,
        admin_username: str,
        prior_raw_token: str | None,
    ) -> int:
        captured_actor["value"] = actor_context_from_request(
            request,
            actor=admin_username,
        ).actor
        return 42

    with (
        _login_flow_ready(),
        patch("app.admin_routes._issue_session", side_effect=_fake_issue_session),
        patch(
            "app.admin_routes.admin_auth.finalize_successful_login",
            return_value=None,
        ),
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": "flow-csrf",
            },
        )
    assert response.status_code == 303
    assert captured_actor["value"] == TEST_USERNAME


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_exclude_candidate_and_secret(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    caplog.set_level(logging.WARNING)
    with (
        _login_flow_ready(),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.crm_transaction") as tx,
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=RuntimeError("audit down"),
        ),
    ):
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        tx.return_value.__enter__.return_value = None
        tx.return_value.__exit__.return_value = None
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
    assert CLIENT_SOURCE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.postgres import PostgresAuditEventRepository

    settings = get_settings()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_SOURCE, settings)
    assert source_key != _plain_sha256_identifier("src", CLIENT_SOURCE.lower())

    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    row = pg_conn.execute(
        "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
        (source_key,),
    ).fetchone()
    assert row is not None
    assert len(row["limiter_key"]) == 64

    repo = PostgresAuditEventRepository()
    from app.actor_context import ActorContext

    audit_service.record_login_failure(
        pg_conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-242"),
        reason="invalid_credentials",
        repository=repo,
    )
    pg_conn.commit()
    audit_row = pg_conn.execute(
        """
        SELECT actor, summary_after, metadata
        FROM audit_events
        WHERE action = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
    ).fetchone()
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    payload = json.dumps(
        {
            "summary_after": audit_row["summary_after"],
            "metadata": audit_row["metadata"],
        }
    )
    assert CANDIDATE_USERNAME not in payload
    assert TEST_USERNAME not in payload
