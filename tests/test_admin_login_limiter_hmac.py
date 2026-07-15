"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum!"

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
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", limiter_secret)
    if previous_secret:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    return get_settings()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


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


@contextmanager
def _mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as route_conn:
        route_conn.return_value.__enter__.return_value = conn
        route_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    source = "203.0.113.50"
    persisted = admin_auth.build_source_rate_limit_key(source, settings)
    plain = admin_auth._plain_sha256_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        source.lower(),
    )
    assert persisted != plain
    assert len(persisted) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = _settings(monkeypatch, limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(monkeypatch, limiter_secret=ALT_LIMITER_SECRET)
    source = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    source = "203.0.113.50"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    payload = "operator"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme" + "x" * 24, "ADMIN_LOGIN_LIMITER_SECRET"),
        ("a" * 32, "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    label: str,
) -> None:
    with pytest.raises(ValueError, match=label):
        admin_auth.validate_admin_login_limiter_secret(secret, label=label)


@pytest.mark.unit
def test_limiter_config_rejects_identical_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_login_limiter_config(settings)


@pytest.mark.unit
def test_startup_validates_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changeme" + "x" * 24)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    with patch("app.main.db.init_db"):
        with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
            with TestClient(app) as test_client:
                test_client.get("/health")


@pytest.mark.integration
def test_rotation_reconciles_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    settings = Settings(
        database_url=_DATABASE_URL,
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=PREVIOUS_LIMITER_SECRET,
    )
    source = "203.0.113.88"
    previous_key = admin_auth.build_source_rate_limit_key(
        source, settings, secret=PREVIOUS_LIMITER_SECRET
    )
    current_key = admin_auth.build_source_rate_limit_key(source, settings)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 3, %s, NULL, %s)
            """,
            (previous_key, now, now),
        )
        pg_conn.commit()

    db.reconcile_rotated_limiter_keys(
        pg_conn,
        key_pairs=((current_key, previous_key),),
        now=now,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key, failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_key,),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        previous_count = cur.fetchone()
    assert row is not None
    assert int(row["failure_count"]) == 3
    assert previous_count is not None
    assert int(previous_count["count"]) == 0


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_key_rows(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    stale_key = "deadbeef" * 8
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (stale_key, now - timedelta(hours=2), now - timedelta(hours=2)),
        )
        pg_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@pytest.mark.integration
def test_hmac_keys_preserve_atomic_admission_threshold(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.77", settings)
    now = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
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


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    candidate = "attacker-candidate@example.com"
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert response.status_code == 401
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"
    assert candidate not in str(failure_audit.call_args)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
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
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure"
                    ) as failure_audit:
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
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
def test_lockout_transition_audit_uses_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    lockout = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=True,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            side_effect=[admitted, lockout],
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    first = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    second = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert first.status_code == 401
    assert second.status_code == 401
    assert failure_audit.call_count == 2
    assert failure_audit.call_args_list[-1].kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args_list[-1].kwargs["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
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
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    candidate = "probe-user@evil.example"
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    caplog.set_level(logging.INFO)
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    combined = caplog.text
    assert candidate not in combined
    assert settings.admin_login_limiter_secret not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source = "203.0.113.199"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = admin_auth._plain_sha256_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        source,
    )
    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-242")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        limiter_row = cur.fetchone()
        cur.execute(
            """
            SELECT actor, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        audit_row = cur.fetchone()

    assert limiter_row is not None
    assert limiter_row["limiter_key"] == source_key
    assert limiter_row["limiter_key"] != plain
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    assert audit_row["metadata"]["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in json.dumps(audit_row["metadata"])
