"""Keyed admin login limiter identifiers and anonymous failure audit actors."""

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
from app.actor_context import ActorContext
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREV = "prev-limiter-secret-32chars-minimum!"
TEST_SOURCE = "203.0.113.42"
TEST_CANDIDATE = "attacker-controlled-name"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_limiter_key(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_secret_previous: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
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
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_secret_previous,
    )


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def _mock_db_connection() -> Iterator[MagicMock]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_db,
        patch("app.admin_auth.db.db_connection") as auth_db,
    ):
        route_db.return_value.__enter__.return_value = conn
        route_db.return_value.__exit__.return_value = None
        auth_db.return_value.__enter__.return_value = conn
        auth_db.return_value.__exit__.return_value = None
        yield conn


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


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert source_key != _plain_sha256_limiter_key("src", TEST_SOURCE.lower())
    assert account_key != _plain_sha256_limiter_key("acct", TEST_USERNAME.lower())
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_LIMITER_SECRET_ALT)
    key_a = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("limiter_secret", "limiter_previous", "env_name"),
    [
        ("", "", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "", "ADMIN_LOGIN_LIMITER_SECRET"),
        (TEST_LIMITER_SECRET, "short-secret", "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    limiter_secret: str,
    limiter_previous: str,
    env_name: str,
) -> None:
    if limiter_secret == "":
        settings = _settings(limiter_secret="", limiter_secret_previous=limiter_previous)
    elif limiter_previous:
        settings = _settings(
            limiter_secret=limiter_secret,
            limiter_secret_previous=limiter_previous,
        )
    else:
        settings = _settings(limiter_secret=limiter_secret)
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_startup_lifespan_validates_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app as fastapi_app, lifespan

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    import asyncio

    async def _run() -> None:
        with patch("app.main.db.init_db"):
            async with lifespan(fastapi_app):
                pass

    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        asyncio.run(_run())


@pytest.mark.unit
def test_rotation_lookup_honors_previous_secret_lockout() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_LIMITER_SECRET_PREV,
    )
    previous_key = admin_auth.build_source_rate_limit_key(
        TEST_SOURCE,
        settings,
        secret=TEST_LIMITER_SECRET_PREV,
    )
    current_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    lookup_keys = admin_auth.login_limiter_lookup_keys(
        submitted_username="ghost",
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert previous_key in lookup_keys
    assert current_key in lookup_keys
    assert previous_key != current_key


@pytest.mark.integration
def test_rotation_denies_admission_while_previous_key_locked(pg_conn: psycopg.Connection) -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_LIMITER_SECRET_PREV,
    )
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    previous_key = admin_auth.build_source_rate_limit_key(
        TEST_SOURCE,
        settings,
        secret=TEST_LIMITER_SECRET_PREV,
    )
    current_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    lockout_until = now + timedelta(seconds=900)
    pg_conn.execute(
        """
        INSERT INTO admin_login_rate_limits (
            limiter_key, failure_count, window_started_at, locked_until, updated_at
        )
        VALUES (%s, 5, %s, %s, %s)
        """,
        (previous_key, 5, now, lockout_until, now),
    )
    pg_conn.commit()

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_key,),
        lookup_keys=(current_key, previous_key),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_expired_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_LIMITER_SECRET_PREV,
    )
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=2)
    previous_key = admin_auth.build_source_rate_limit_key(
        TEST_SOURCE,
        settings,
        secret=TEST_LIMITER_SECRET_PREV,
    )
    pg_conn.execute(
        """
        INSERT INTO admin_login_rate_limits (
            limiter_key, failure_count, window_started_at, locked_until, updated_at
        )
        VALUES (%s, 1, %s, NULL, %s)
        """,
        (previous_key, stale, stale),
    )
    pg_conn.commit()

    removed = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert removed == 1


@pytest.mark.integration
def test_concurrent_admission_with_hmac_keys_respects_threshold(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.88", settings)
    now = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
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
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-1"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert TEST_CANDIDATE not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-2"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-3"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_capture):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": TEST_PASSWORD,
                        "csrf_token": "bad-csrf",
                    },
                )
    assert response.status_code == 400
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor() -> None:
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    login = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert login.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME


@pytest.mark.unit
def test_login_failure_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    with _mock_db_connection():
        with patch("app.admin_routes.crm_transaction", side_effect=RuntimeError("store down")):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": TEST_CANDIDATE,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert TEST_CANDIDATE not in joined
    assert TEST_LIMITER_SECRET not in joined
    assert TEST_SOURCE not in joined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
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
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != _plain_sha256_limiter_key("src", TEST_SOURCE.lower())

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-242")
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
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
        },
        default=str,
    )
    assert TEST_CANDIDATE not in payload
