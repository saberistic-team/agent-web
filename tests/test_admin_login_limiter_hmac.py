"""Keyed HMAC limiter identifiers and anonymous failed-login audit actors (#242)."""

from __future__ import annotations

import hashlib
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
from app.actor_context import anonymous_actor_context
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    admin_env,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

_SECRET_A = "rotation-limiter-secret-a-32bytes!!"
_SECRET_B = "rotation-limiter-secret-b-32bytes!!"
_CANDIDATE_USERNAME = "attacker-controlled-name"
_CANDIDATE_IP = "203.0.113.42"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings_with_secret(
    secret: str,
    *,
    previous: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
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


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter HMAC tests")


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


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings_with_secret(TEST_LIMITER_SECRET)
    source_key = admin_auth.build_source_rate_limit_key(_CANDIDATE_IP, settings)
    account_key = admin_auth.build_account_rate_limit_key(_CANDIDATE_USERNAME, settings)
    assert source_key != _plain_sha256_limiter_key("src", _CANDIDATE_IP.lower())
    assert account_key != _plain_sha256_limiter_key("acct", _CANDIDATE_USERNAME.lower())


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secret(_SECRET_A)
    settings_b = _settings_with_secret(_SECRET_B)
    material = "203.0.113.9"
    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    settings = _settings_with_secret(TEST_LIMITER_SECRET)
    first = admin_auth.build_source_rate_limit_key("198.51.100.1", settings)
    second = admin_auth.build_source_rate_limit_key("198.51.100.1", settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation_for_identical_payload() -> None:
    settings = _settings_with_secret(TEST_LIMITER_SECRET)
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-" + "x" * 24, "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth._validate_limiter_secret_value(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_startup_validation_requires_secret_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_login_limiter_config(settings)


@pytest.mark.unit
def test_limiter_previous_secret_must_differ_from_current() -> None:
    settings = _settings_with_secret(_SECRET_A, previous=_SECRET_A)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_login_limiter_config(settings)


@pytest.mark.unit
def test_rotation_admits_against_previous_key_rows() -> None:
    settings_old = _settings_with_secret(_SECRET_A)
    settings_rotated = _settings_with_secret(_SECRET_B, previous=_SECRET_A)
    old_key = admin_auth.build_source_rate_limit_key(_CANDIDATE_IP, settings_old)
    rotated_keys = admin_auth.login_limiter_keys(
        submitted_username="ghost",
        client_source=_CANDIDATE_IP,
        configured_admin_username=TEST_USERNAME,
        settings=settings_rotated,
    )
    assert old_key in rotated_keys
    assert admin_auth.build_source_rate_limit_key(_CANDIDATE_IP, settings_rotated) in rotated_keys


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    settings_old = _settings_with_secret(_SECRET_A)
    old_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings_old)
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(old_key,),
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
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (old_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
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
        captured["metadata"] = {"reason": reason}

    with mock_db_connection():
        with (
            patch("app.admin_routes._try_claim_login_flow", return_value=True),
            patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture),
        ):
            response = client.post(
                "/admin/login",
                data={
                    "username": _CANDIDATE_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert response.status_code == 401
    assert captured["actor"] == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert _CANDIDATE_USERNAME not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_actor_is_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}

    with mock_db_connection():
        with (
            patch("app.admin_routes._try_claim_login_flow", return_value=True),
            patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit,
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
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert TEST_USERNAME not in failure_audit.call_args.kwargs


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous() -> None:
    with mock_db_connection():
        with patch(
            "app.admin_routes.audit_service.record_login_failure"
        ) as failure_audit:
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "tampered-csrf",
                },
            )
    assert response.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch("app.admin_routes._record_login_failure") as audit_mock:
                assert _login(password="wrong").status_code == 401
                lockout = _login(password="wrong")
                assert lockout.status_code == 401
                assert audit_mock.call_args_list[-1].kwargs == {"reason": "rate_limited"}


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
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
def test_failed_login_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login

    caplog.set_level(logging.INFO)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            response = _login(username=_CANDIDATE_USERNAME, password="wrong-password")
    assert response.status_code == 401
    combined = caplog.text
    for forbidden in (
        _CANDIDATE_USERNAME,
        TEST_LIMITER_SECRET,
        _CANDIDATE_IP,
        "src:",
        "acct:",
    ):
        assert forbidden not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key(_CANDIDATE_IP, settings)
    assert source_key != _plain_sha256_limiter_key("src", _CANDIDATE_IP.lower())

    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
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

    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "corr-pg-failure"
    actor_context = anonymous_actor_context(request)
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor_context,
            reason="invalid_credentials",
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
    serialized = str(audit_row)
    assert _CANDIDATE_USERNAME not in serialized
    assert TEST_LIMITER_SECRET not in serialized


@pytest.mark.integration
def test_concurrent_admission_with_hmac_keys(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.77", settings)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
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
