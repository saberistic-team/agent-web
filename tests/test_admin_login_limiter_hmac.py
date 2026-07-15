"""Tests for HMAC login limiter identifiers and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import AdminSecurityConfigurationError, validate_admin_security_settings
from app.config import Settings
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min!!"
OTHER_LIMITER_SECRET = "other-login-limiter-secret-32chars-min!"
PREVIOUS_LIMITER_SECRET = "prev-login-limiter-secret-32chars-min!!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_previous: str = "",
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
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_previous,
    )


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


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


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
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = admin_auth._plain_sha256_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        source,
    )
    assert key != plain
    assert len(key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    material = "203.0.113.10"
    current = admin_auth.build_source_rate_limit_key(material, _settings(limiter_secret=TEST_LIMITER_SECRET))
    other = admin_auth.build_source_rate_limit_key(material, _settings(limiter_secret=OTHER_LIMITER_SECRET))
    assert current != other


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared = "operator"
    source_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        shared,
        settings.admin_login_limiter_secret,
    )
    account_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_ACCOUNT,
        shared,
        settings.admin_login_limiter_secret,
    )
    assert source_key != account_key


@pytest.mark.unit
def test_missing_limiter_secret_fails_startup_validation() -> None:
    settings = _settings(limiter_secret="")
    with pytest.raises(AdminSecurityConfigurationError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_settings(settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    [
        "short",
        "changeme",
        "placeholder",
    ],
)
def test_weak_limiter_secret_fails_startup_validation(secret: str) -> None:
    settings = _settings(limiter_secret=secret)
    with pytest.raises(AdminSecurityConfigurationError):
        validate_admin_security_settings(settings)


@pytest.mark.unit
def test_rotation_previous_secret_must_differ() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(AdminSecurityConfigurationError, match="must differ"):
        validate_admin_security_settings(settings)


@pytest.mark.integration
def test_rotation_honors_previous_key_lockout(
    pg_conn: psycopg.Connection,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=PREVIOUS_LIMITER_SECRET,
    )
    now = datetime.now(timezone.utc)
    legacy_keys = admin_auth.legacy_login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.88",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert legacy_keys
    for _ in range(5):
        db.try_admit_admin_login(
            pg_conn,
            limiter_keys=legacy_keys,
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host="203.0.113.88")
    admission = admin_auth.try_admit_login_attempt(
        request,
        settings,
        username=TEST_USERNAME,
    )
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    settings = _settings(limiter_secret=OTHER_LIMITER_SECRET)
    now = datetime.now(timezone.utc)
    legacy_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.77",
        PREVIOUS_LIMITER_SECRET,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (legacy_key, now - timedelta(seconds=3600), now - timedelta(seconds=3600)),
        )
        pg_conn.commit()
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (legacy_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.integration
def test_pg_persisted_limiter_rows_use_hmac_keys(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime.now(timezone.utc)
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.55",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=keys,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    with pg_conn.cursor() as cur:
        cur.execute("SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key")
        rows = cur.fetchall()
    persisted = {str(row["limiter_key"]) for row in rows}
    assert persisted == set(keys)
    for key in persisted:
        assert len(key) == 64
        assert key == key.lower()
        plain_guess = hashlib.sha256(b"src:203.0.113.55").hexdigest()
        assert key != plain_guess


class _AuditSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def append(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"id": len(self.calls), **kwargs}


@contextmanager
def _login_mocks(*, claim: bool = True) -> Generator[_AuditSpy, None, None]:
    spy = _AuditSpy()
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )

    real_record_login_failure = audit_service.record_login_failure

    def _capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any] | None:
        return real_record_login_failure(
            conn,
            actor_context=kwargs["actor_context"],
            reason=kwargs["reason"],
            repository=spy,
        )

    with (
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes._try_claim_login_flow", return_value=claim),
        patch("app.admin_routes._try_burn_login_flow_cookie", return_value=True),
        patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted),
        patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture_failure),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.crm_transaction") as crm_tx,
    ):
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        crm_tx.return_value.__enter__.return_value = None
        crm_tx.return_value.__exit__.return_value = None
        yield spy


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_is_anonymous() -> None:
    candidate = "attacker-supplied-username"
    with _login_mocks() as spy:
        response = client.post(
            "/admin/login",
            data={
                "username": candidate,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
    assert response.status_code == 401
    assert spy.calls
    event = spy.calls[-1]
    assert event["actor"] == "anonymous"
    payload = json.dumps(event)
    assert candidate not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_is_anonymous() -> None:
    with _login_mocks() as spy:
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
    assert response.status_code == 401
    event = spy.calls[-1]
    assert event["actor"] == "anonymous"
    assert TEST_USERNAME not in json.dumps(event)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous() -> None:
    with _login_mocks(claim=False) as spy:
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": "bad-csrf",
            },
        )
    assert response.status_code == 400
    event = spy.calls[-1]
    assert event["actor"] == "anonymous"
    assert event["metadata"] == {"reason": "invalid_csrf"}


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    with (
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes._try_claim_login_flow", return_value=True),
        patch("app.admin_routes.db.create_admin_session", return_value=42),
        patch("app.admin_routes.audit_service.record_login_success") as success_audit,
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.crm_transaction") as crm_tx,
    ):
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        crm_tx.return_value.__enter__.return_value = None
        crm_tx.return_value.__exit__.return_value = None
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
def test_failed_login_logs_do_not_leak_candidates(caplog: pytest.LogCaptureFixture) -> None:
    candidate = "leaked-candidate-user"
    caplog.set_level(logging.INFO)
    with _login_mocks():
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
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.integration
def test_pg_audit_login_failure_row_is_anonymous(pg_conn: psycopg.Connection) -> None:
    from app.actor_context import anonymous_actor_context

    request = MagicMock()
    request.headers = {}
    request.state = MagicMock(correlation_id="corr-login-failure")
    actor_context = anonymous_actor_context(request)
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor_context,
        reason="invalid_credentials",
        repository=PostgresAuditEventRepository(),
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = 'auth.login.failure'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert row["summary_after"]["reason"] == "invalid_credentials"
    assert row["metadata"]["reason"] == "invalid_credentials"
    assert "username" not in json.dumps(row).lower()


@pytest.mark.integration
def test_concurrent_admission_respects_threshold_with_hmac_keys(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    settings = _settings()
    now = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.66", settings)
    rate_limit = 5
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
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
    assert len(source_key) == 64
