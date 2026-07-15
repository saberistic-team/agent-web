"""PostgreSQL integration tests for keyed admin login limiter identifiers."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.rows import dict_row

from app import admin_auth, db
from app.admin_routes import _record_login_failure
from app.admin_security import LIMITER_DOMAIN_SOURCE, digest_limiter_identifier
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations
from starlette.requests import Request

from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import TEST_SECRET, TEST_USERNAME

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


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


@pytest.fixture
def pg_env(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash(TEST_PASSWORD))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.integration
def test_persisted_limiter_rows_use_keyed_identifiers(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    settings = get_settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert source_key != plain
    assert len(source_key) == 64

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
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
            "SELECT limiter_key, failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert int(row["failure_count"]) == 1


@pytest.mark.integration
def test_rotation_window_preserves_lockout_state(
    pg_conn: psycopg.Connection,
    pg_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_secret = "previous-limiter-secret-32chars-minimum"
    current_secret = "current-limiter-secret-32chars-minimum"
    source = "203.0.113.99"
    now = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)

    old_settings = Settings(
        database_url=get_settings().database_url,
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
        admin_password_hash=get_settings().admin_password_hash,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=previous_secret,
    )
    old_key = admin_auth.build_source_rate_limit_key(source, old_settings)

    for index in range(5):
        admission = db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(old_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", current_secret)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", previous_secret)
    rotated_settings = get_settings()
    rotation_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=rotated_settings,
    )
    assert old_key in rotation_keys

    blocked = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=rotation_keys,
        now=now + timedelta(seconds=30),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked


@pytest.mark.integration
def test_failed_login_audit_persists_anonymous_actor_only(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    candidate = "attacker-supplied-username"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [(b"x-request-id", b"audit-corr-1")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.correlation_id = "audit-corr-1"
    _ = candidate

    @contextmanager
    def _pg_db_connection(_database_url: str) -> Iterator[psycopg.Connection]:
        yield pg_conn

    with patch("app.admin_routes.db.db_connection", _pg_db_connection):
        _record_login_failure(request, reason="invalid_credentials")
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, action, summary_after, metadata
            FROM audit_events
            WHERE action = 'auth.login.failure'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    serialized = str(row["summary_after"]) + str(row["metadata"])
    assert candidate not in serialized


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_key_rows(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    retired_secret = b"retired-limiter-secret-32chars-minimum-x"
    source = "203.0.113.70"
    retired_key = digest_limiter_identifier(
        LIMITER_DOMAIN_SOURCE,
        source.strip().lower(),
        retired_secret,
    )
    stale_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (retired_key, stale_time, stale_time),
        )
        pg_conn.commit()

    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
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
            (retired_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0
