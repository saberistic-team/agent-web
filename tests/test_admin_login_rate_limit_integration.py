"""PostgreSQL integration tests for atomic admin login admission."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, db
from app.config import get_settings
from app.migrations.runner import apply_migrations

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


def _settings():
    return get_settings()


@pytest.mark.integration
def test_username_rotation_shares_source_bucket(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
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
    settings = _settings()
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
    settings = _settings()
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    now = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    for index in range(5):
        source_key = admin_auth.build_source_rate_limit_key(
            f"203.0.113.{index + 1}", settings
        )
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
    settings = _settings()
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
    settings = _settings()
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
    settings = _settings()
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


def _plain_sha256_limiter_digest(prefix: str, material: str) -> str:
    import hashlib

    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.integration
def test_hmac_limiter_keys_persist_without_plain_sha256(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source = "203.0.113.242"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = _plain_sha256_limiter_digest("src", source.lower())
    assert source_key != plain
    assert len(source_key) == 64

    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    _admit(pg_conn, keys=(source_key,), now=now, rate_limit=5)
    pg_conn.commit()

    row = pg_conn.execute(
        "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
        (source_key,),
    ).fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_secret_rows(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.88",
        settings,
        secret="previous-login-limiter-secret-32chars!!",
    )
    stale_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pg_conn.execute(
        """
        INSERT INTO admin_login_rate_limits (
            limiter_key, failure_count, window_started_at, locked_until, updated_at
        ) VALUES (%s, 1, %s, NULL, %s)
        """,
        (previous_source, stale_time, stale_time),
    )
    pg_conn.commit()
    assert _count_limiter_rows(pg_conn) == 1

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=stale_time + timedelta(seconds=2000),
        window_seconds=60,
        lockout_seconds=60,
    )
    pg_conn.commit()
    assert deleted >= 1
    assert _count_limiter_rows(pg_conn) == 0
