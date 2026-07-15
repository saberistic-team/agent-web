"""PostgreSQL integration tests for atomic admin login admission."""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import get_settings
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository

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
    # App helpers expect dict rows; apply_migrations needs default tuple rows.
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


def _migrate(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=False) as conn:
        apply_migrations(conn)


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


def _count_limiter_rows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _admit(
    conn: psycopg.Connection,
    *,
    keys: tuple[str, ...],
    now: datetime,
    rate_limit: int = 5,
    window_seconds: int = 900,
    lockout_seconds: int = 900,
) -> db.AdminLoginAdmission:
    return db.try_admit_admin_login(
        conn,
        limiter_keys=keys,
        now=now,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
    )


@pytest.mark.integration
def test_username_rotation_shares_source_bucket(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)

    for index in range(5):
        user_key = admin_auth.build_rate_limit_key(f"user-{index}", "203.0.113.10")
        assert user_key != source_key
        admission = _admit(
            pg_conn,
            keys=(source_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
        )
        assert admission.admitted

    blocked = _admit(
        pg_conn,
        keys=(source_key,),
        now=now + timedelta(seconds=10),
        rate_limit=5,
    )
    assert not blocked.admitted
    assert blocked.already_locked
    assert _count_limiter_rows(pg_conn) == 1


@pytest.mark.integration
def test_concurrent_admission_does_not_overshoot_threshold(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.20", settings)
    now = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    rate_limit = 5
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with _connect(_DATABASE_URL) as conn:
            admission = _admit(
                conn,
                keys=(source_key,),
                now=now,
                rate_limit=rate_limit,
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


@pytest.mark.integration
def test_account_bucket_limits_configured_admin_across_sources(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    now = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    for index in range(5):
        source_key = admin_auth.build_source_rate_limit_key(f"203.0.113.{index + 1}", settings)
        admission = _admit(
            pg_conn,
            keys=(source_key, account_key),
            now=now + timedelta(seconds=index),
            rate_limit=5,
        )
        assert admission.admitted

    blocked_source = admin_auth.build_source_rate_limit_key("203.0.113.99", settings)
    blocked = _admit(
        pg_conn,
        keys=(blocked_source, account_key),
        now=now + timedelta(seconds=20),
        rate_limit=5,
    )
    assert not blocked.admitted
    assert blocked.already_locked


@pytest.mark.integration
def test_window_boundary_resets_failure_count(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.44", settings)
    window_seconds = 60
    start = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)

    for index in range(4):
        admission = _admit(
            pg_conn,
            keys=(source_key,),
            now=start + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=window_seconds,
            lockout_seconds=900,
        )
        assert admission.admitted

    after_window = start + timedelta(seconds=window_seconds + 1)
    admission = _admit(
        pg_conn,
        keys=(source_key,),
        now=after_window,
        rate_limit=5,
        window_seconds=window_seconds,
        lockout_seconds=900,
    )
    assert admission.admitted
    assert not admission.lockout_transition


@pytest.mark.integration
def test_expired_lockout_allows_new_admissions(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.55", settings)
    start = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    lockout_seconds = 30

    for index in range(5):
        _admit(
            pg_conn,
            keys=(source_key,),
            now=start + timedelta(seconds=index),
            rate_limit=5,
            lockout_seconds=lockout_seconds,
        )

    blocked = _admit(
        pg_conn,
        keys=(source_key,),
        now=start + timedelta(seconds=6),
        rate_limit=5,
        lockout_seconds=lockout_seconds,
    )
    assert not blocked.admitted

    # Lockout begins at the threshold transition (index 4 => start+4s), not at `start`.
    after_lockout = start + timedelta(seconds=4 + lockout_seconds + 1)
    allowed = _admit(
        pg_conn,
        keys=(source_key,),
        now=after_lockout,
        rate_limit=5,
        lockout_seconds=lockout_seconds,
    )
    assert allowed.admitted


@pytest.mark.integration
def test_cleanup_removes_stale_unlocked_rows(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.66", settings)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    _admit(pg_conn, keys=(source_key,), now=now, rate_limit=5, window_seconds=60)

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    assert _count_limiter_rows(pg_conn) == 0


@pytest.mark.integration
def test_persisted_limiter_keys_use_hmac_not_plain_sha256(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    _admit(pg_conn, keys=(source_key,), now=now, rate_limit=5)
    pg_conn.commit()

    row = pg_conn.execute(
        "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
        (source_key,),
    ).fetchone()
    assert row is not None
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain


@pytest.mark.integration
def test_rotation_previous_key_lock_blocks_without_current_row(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_secret = "previous-limiter-secret-32chars-minimum"
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    settings = get_settings()
    source = "203.0.113.77"
    previous_key = admin_auth._digest_limiter_key(
        previous_secret.encode("utf-8"),
        admin_auth.LIMITER_KEY_DOMAIN_SRC,
        source,
    )
    current_key = admin_auth.build_source_rate_limit_key(source, settings)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)

    for index in range(5):
        _admit(
            pg_conn,
            keys=(previous_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
        )
    pg_conn.commit()

    assert _any_locked(pg_conn, previous_key, now + timedelta(seconds=10))
    assert pg_conn.execute(
        "SELECT 1 FROM admin_login_rate_limits WHERE limiter_key = %s",
        (current_key,),
    ).fetchone() is None

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
        "client": (source, 12345),
        "server": ("testserver", 80),
    }
    from starlette.requests import Request

    request = Request(scope)
    result = admin_auth.try_admit_login_attempt(request, settings, username="ghost")
    assert not result.admitted
    assert result.already_locked


def _any_locked(conn: psycopg.Connection, limiter_key: str, now: datetime) -> bool:
    return db.is_admin_login_throttled(conn, limiter_key=limiter_key, now=now)


@pytest.mark.integration
def test_login_failure_audit_persists_anonymous_actor(pg_conn: psycopg.Connection) -> None:
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-login-failure")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=PostgresAuditEventRepository(),
    )
    pg_conn.commit()

    row = pg_conn.execute(
        """
        SELECT actor, summary_after, metadata
        FROM audit_events
        WHERE action = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
    ).fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert row["summary_after"]["reason"] == "invalid_credentials"
    serialized = str(row)
    assert "operator" not in serialized
    assert "ghost" not in serialized
