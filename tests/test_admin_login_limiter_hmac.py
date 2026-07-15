"""Tests for HMAC-SHA-256 admin login limiter keys and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.admin_secrets import (
    MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES,
    validate_admin_login_limiter_secret,
    validate_admin_security_secrets,
)
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum"

client = TestClient(app, follow_redirects=False)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    previous_secret: str = "",
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", limiter_secret)
    if previous_secret:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()
    return get_settings()


def _plain_sha256(prefix: str, material: str) -> str:
    return hashlib.sha256(f"{prefix}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256("src", "203.0.113.1")
    assert account_key != _plain_sha256("acct", "operator")
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = _settings(monkeypatch, limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(monkeypatch, limiter_secret=ALT_LIMITER_SECRET)
    key_a = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_a)
    key_b = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("placeholder", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(ValueError):
        validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_validate_admin_security_secrets_rejects_matching_rotation_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_startup_validation_fails_for_missing_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_throttle_check_includes_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=ALT_LIMITER_SECRET,
        previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    keys = admin_auth.throttle_check_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    previous_source = admin_auth._hmac_limiter_key(
        "src", "203.0.113.1", PREVIOUS_LIMITER_SECRET
    )
    assert current_source in keys
    assert previous_source in keys
    admission_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert previous_source not in admission_keys


@pytest.mark.unit
def test_rotation_honors_previous_lock_without_incrementing_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    settings = _settings(
        monkeypatch,
        limiter_secret=ALT_LIMITER_SECRET,
        previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    store = FakeRateLimitStore()
    now = datetime.now(timezone.utc)
    previous_source = admin_auth._hmac_limiter_key(
        "src", "testclient", PREVIOUS_LIMITER_SECRET
    )
    store.rows[previous_source] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }

    with shared_rate_limiter(store):
        with patch("app.admin_auth.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = MagicMock()
            db_conn.return_value.__exit__.return_value = None
            request = MagicMock()
            request.cookies = {}
            request.headers = {}
            request.state = MagicMock(correlation_id="corr-rotation")
            with patch(
                "app.admin_auth.client_ip",
                return_value="testclient",
            ):
                result = admin_auth.try_admit_login_attempt(
                    request,
                    settings,
                    username=TEST_USERNAME,
                )

    assert not result.admitted
    assert result.already_locked
    current_source = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert current_source not in store.rows


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
def pg_conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    _settings(monkeypatch)
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
def test_hmac_limiter_rows_persist_in_postgres(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
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
    assert row["limiter_key"] != _plain_sha256("src", "203.0.113.88")


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=ALT_LIMITER_SECRET,
        previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    previous_key = admin_auth._hmac_limiter_key("src", "203.0.113.90", PREVIOUS_LIMITER_SECRET)
    stale_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, stale_time, stale_time),
        )
    removed = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert removed >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


class _AuditSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(
        self,
        conn: Any,
        *,
        actor: str,
        action: str,
        correlation_id: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        summary_before: dict[str, Any] | None = None,
        summary_after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "actor": actor,
            "action": action,
            "correlation_id": correlation_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "summary_before": summary_before,
            "summary_after": summary_after,
            "metadata": metadata,
        }
        self.events.append(event)
        return {"id": len(self.events)}


@pytest.mark.unit
def test_record_login_failure_invokes_anonymous_actor_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.actor_context import ActorContext
    from app.admin_routes import _record_login_failure
    from tests.test_admin_auth import mock_db_connection

    _settings(monkeypatch)
    request = MagicMock()
    anonymous = ActorContext(actor="anonymous", correlation_id="corr-anon")

    with mock_db_connection():
        with patch(
            "app.admin_routes.anonymous_actor_context",
            return_value=anonymous,
        ) as anon_mock:
            with patch("app.admin_routes.audit_service.record_login_failure") as record_mock:
                _record_login_failure(request, reason="invalid_credentials")

    anon_mock.assert_called_once_with(request)
    record_mock.assert_called_once()
    assert record_mock.call_args.kwargs["actor_context"] == anonymous
    assert record_mock.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
def test_record_login_failure_repository_spy_persists_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.actor_context import ActorContext

    _settings(monkeypatch)
    spy = _AuditSpy()
    conn = MagicMock()

    audit_service.record_login_failure(
        conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-anon"),
        reason="invalid_credentials",
        repository=spy,
    )

    assert len(spy.events) == 1
    event = spy.events[0]
    assert event["actor"] == "anonymous"
    assert event["summary_after"] == {"reason": "invalid_credentials"}
    assert "username" not in json.dumps(event).lower()


@pytest.mark.unit
def test_unknown_username_failure_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    _settings(monkeypatch)
    store = FakeRateLimitStore()
    candidate = "attacker-controlled-name"

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._record_login_failure") as audit_mock:
                with mock_db_connection():
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "placeholder",
                        },
                    )
    assert response.status_code in {400, 401}
    assert audit_mock.call_count >= 1
    assert audit_mock.call_args.kwargs["reason"] in {
        "invalid_credentials",
        "invalid_csrf",
    }
    assert "attempted_username" not in audit_mock.call_args.kwargs


@pytest.mark.unit
def test_configured_username_wrong_password_keeps_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _fetch_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    _settings(monkeypatch)
    store = FakeRateLimitStore()

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._record_login_failure") as audit_mock:
                csrf_token, cookies = _fetch_login_form()
                with mock_db_connection():
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
    assert audit_mock.call_count == 1
    assert audit_mock.call_args.kwargs["reason"] == "invalid_credentials"
    assert "attempted_username" not in audit_mock.call_args.kwargs


@pytest.mark.unit
def test_throttled_admission_writes_no_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _fetch_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    _settings(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    now = datetime.now(timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key("testclient")
    store.rows[source_key] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._record_login_failure") as audit_mock:
                csrf_token, cookies = _fetch_login_form()
                with mock_db_connection():
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-supplied-name",
                            "password": "wrong-password",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
    assert response.status_code == 429
    audit_mock.assert_not_called()


@pytest.mark.unit
def test_rate_limited_lockout_transition_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _fetch_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    _settings(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure") as record_mock:
                csrf_token, cookies = _fetch_login_form()
                with mock_db_connection():
                    client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
                    with mock_db_connection():
                        client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": "wrong-password",
                                "csrf_token": csrf_token,
                            },
                            cookies=cookies,
                        )
    rate_limited_calls = [
        call
        for call in record_mock.call_args_list
        if call.kwargs.get("reason") == "rate_limited"
    ]
    assert len(rate_limited_calls) == 1
    assert rate_limited_calls[0].kwargs["actor_context"].actor == "anonymous"
    assert "attempted_username" not in rate_limited_calls[0].kwargs


@pytest.mark.unit
def test_invalid_csrf_failure_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _fetch_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    _settings(monkeypatch)
    store = FakeRateLimitStore()

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._record_login_failure") as audit_mock:
                csrf_token, cookies = _fetch_login_form()
                with mock_db_connection():
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": csrf_token + "tampered",
                        },
                        cookies=cookies,
                    )
    assert response.status_code == 400
    assert audit_mock.call_count == 1
    assert audit_mock.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
def test_successful_login_retains_authenticated_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _extract_session_cookie,
        _fetch_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    _settings(monkeypatch)
    spy = _AuditSpy()
    store = FakeRateLimitStore()
    record_success = audit_service.record_login_success

    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_success",
                side_effect=lambda conn, **kwargs: record_success(
                    conn,
                    actor_context=kwargs["actor_context"],
                    session_id=kwargs["session_id"],
                    repository=spy,
                ),
            ):
                csrf_token, cookies = _fetch_login_form()
                with mock_db_connection():
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
    success_events = [
        event
        for event in spy.events
        if event["action"] == audit_service.ACTION_AUTH_LOGIN_SUCCESS
    ]
    assert success_events
    assert success_events[-1]["actor"] == TEST_USERNAME
    session_cookie = _extract_session_cookie(response)
    assert session_cookie


@pytest.mark.unit
def test_login_failure_logs_exclude_candidates_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    _settings(monkeypatch)
    store = FakeRateLimitStore()
    candidate = "secret-candidate-user"
    caplog.set_level(logging.INFO)

    with shared_rate_limiter(store):
        with mock_db_connection():
            with caplog.at_level(logging.INFO):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "placeholder",
                    },
                )
    assert response.status_code in {400, 401}
    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_audit_row_uses_anonymous_actor_for_failure(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.actor_context import ActorContext

    _settings(monkeypatch)
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-failure")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert row["summary_after"]["reason"] == "invalid_credentials"
    assert "username" not in json.dumps(row["summary_after"]).lower()
    assert "username" not in json.dumps(row["metadata"]).lower()


@pytest.mark.integration
def test_concurrent_hmac_admission_respects_threshold(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.77", settings)
    now = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    rate_limit = 5
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with _connect(_DATABASE_URL) as conn:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=rate_limit,
                window_seconds=900,
                lockout_seconds=900,
            )
            if admission.admitted:
                with lock:
                    admitted_count["value"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert admitted_count["value"] == rate_limit
