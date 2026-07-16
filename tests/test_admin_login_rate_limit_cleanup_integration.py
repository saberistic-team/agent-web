"""PostgreSQL integration tests for bounded admin login rate-limit cleanup (#332)."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, db
from app.migrations.runner import apply_migrations

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

_TEST_BATCH_SIZE = 10
_WINDOW_SECONDS = 60
_LOCKOUT_SECONDS = 60
_RETENTION_SECONDS = max(_WINDOW_SECONDS, _LOCKOUT_SECONDS) * 2
_EXPLAIN_ROW_COUNT = 1000
_EXPLAIN_TIME_BUDGET_SECONDS = 2.0

pytestmark = [pytest.mark.integration]


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres rate-limit cleanup tests")


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


def _insert_limiter_row(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    updated_at: datetime,
    locked_until: datetime | None = None,
    failure_count: int = 1,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (limiter_key, failure_count, updated_at, locked_until, updated_at),
        )
    conn.commit()


def _list_limiter_keys(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key"
        )
        rows = cur.fetchall()
    return [str(row["limiter_key"]) for row in rows]


def _cleanup(
    conn: psycopg.Connection,
    *,
    now: datetime,
    batch_size: int = _TEST_BATCH_SIZE,
) -> int:
    return db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=_WINDOW_SECONDS,
        lockout_seconds=_LOCKOUT_SECONDS,
        batch_size=batch_size,
    )


def _seed_expired_rows(
    conn: psycopg.Connection,
    *,
    count: int,
    now: datetime,
    key_prefix: str = "expired",
) -> list[str]:
    stale_at = now - timedelta(seconds=_RETENTION_SECONDS + 60)
    keys: list[str] = []
    for index in range(count):
        key = f"{key_prefix}-{index:04d}"
        _insert_limiter_row(conn, limiter_key=key, updated_at=stale_at + timedelta(seconds=index))
        keys.append(key)
    return keys


def test_cleanup_deletes_at_most_batch_size_oldest_first(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    expired_keys = _seed_expired_rows(pg_conn, count=35, now=now)
    active_key = "active-recent"
    locked_key = "active-locked"
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=30),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=locked_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 120),
        locked_until=now + timedelta(minutes=5),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE
    remaining = set(_list_limiter_keys(pg_conn))
    assert active_key in remaining
    assert locked_key in remaining
    for key in expired_keys[:_TEST_BATCH_SIZE]:
        assert key not in remaining
    for key in expired_keys[_TEST_BATCH_SIZE:]:
        assert key in remaining


def test_repeated_cleanup_drains_only_eligible_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    _seed_expired_rows(pg_conn, count=35, now=now)
    active_key = "still-active"
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=10),
    )

    total_deleted = 0
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        total_deleted += deleted
        if deleted == 0:
            break

    assert total_deleted == 35
    assert _list_limiter_keys(pg_conn) == [active_key]


def test_retention_and_locked_until_boundaries_are_deterministic(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(seconds=_RETENTION_SECONDS)
    just_eligible = "boundary-eligible"
    just_ineligible = "boundary-ineligible"
    lockout_expired = "lockout-expired"
    lockout_active = "lockout-active"

    _insert_limiter_row(
        pg_conn,
        limiter_key=just_eligible,
        updated_at=cutoff - timedelta(seconds=1),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=just_ineligible,
        updated_at=cutoff,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=lockout_expired,
        updated_at=cutoff - timedelta(seconds=10),
        locked_until=now - timedelta(seconds=1),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=lockout_active,
        updated_at=cutoff - timedelta(seconds=10),
        locked_until=now + timedelta(seconds=1),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 2
    remaining = _list_limiter_keys(pg_conn)
    assert just_eligible not in remaining
    assert lockout_expired not in remaining
    assert just_ineligible in remaining
    assert lockout_active in remaining


def test_concurrent_cleanup_claims_disjoint_batches(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    _seed_expired_rows(pg_conn, count=40, now=now)
    barrier = threading.Barrier(2, timeout=10)
    deleted_counts: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        with lock:
            deleted_counts.append(deleted)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(deleted_counts) == [_TEST_BATCH_SIZE, _TEST_BATCH_SIZE]
    assert len(_list_limiter_keys(pg_conn)) == 20


def test_concurrent_admission_prevents_deleting_active_row(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    active_key = "active-during-admission"
    stale_at = now - timedelta(seconds=_RETENTION_SECONDS + 120)
    _insert_limiter_row(pg_conn, limiter_key=active_key, updated_at=stale_at)
    admission_started = threading.Event()
    admission_may_commit = threading.Event()
    cleanup_done = threading.Event()

    def slow_admission() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    """
                    SELECT failure_count, window_started_at, locked_until
                    FROM admin_login_rate_limits
                    WHERE limiter_key = %s
                    FOR UPDATE
                    """,
                    (active_key,),
                )
                row = cur.fetchone()
                assert row is not None
                cur.execute(
                    """
                    UPDATE admin_login_rate_limits
                    SET failure_count = failure_count + 1,
                        updated_at = %s
                    WHERE limiter_key = %s
                    """,
                    (now, active_key),
                )
            admission_started.set()
            admission_may_commit.wait(timeout=10)
            conn.commit()

    def cleanup_worker() -> None:
        admission_started.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        cleanup_done.set()
        assert deleted == 0

    admission_thread = threading.Thread(target=slow_admission)
    cleanup_thread = threading.Thread(target=cleanup_worker)
    admission_thread.start()
    cleanup_thread.start()
    cleanup_thread.join(timeout=15)
    assert cleanup_done.is_set()
    assert active_key in _list_limiter_keys(pg_conn)
    admission_may_commit.set()
    admission_thread.join(timeout=15)


def test_rollback_leaves_rows_claimable_by_later_cleanup(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    keys = _seed_expired_rows(pg_conn, count=12, now=now)

    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                """
                DELETE FROM admin_login_rate_limits
                WHERE limiter_key IN (
                    SELECT limiter_key
                    FROM admin_login_rate_limits
                    WHERE updated_at < %s - make_interval(secs => %s)
                      AND (locked_until IS NULL OR locked_until < %s)
                    ORDER BY updated_at ASC, limiter_key ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                """,
                (now, _RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
            )
            assert cur.rowcount == _TEST_BATCH_SIZE
            conn.rollback()

    assert _list_limiter_keys(pg_conn) == keys
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE


def test_previous_secret_hmac_rows_deleted_by_age_without_secret(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    stale_key = "deadbeef" * 8
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 60),
    )
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert _list_limiter_keys(pg_conn) == []


def test_large_cardinality_cleanup_uses_updated_at_index(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    _seed_expired_rows(pg_conn, count=_EXPLAIN_ROW_COUNT, now=now)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("ANALYZE admin_login_rate_limits")
    pg_conn.commit()
    retention_seconds = _RETENTION_SECONDS

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            EXPLAIN (FORMAT TEXT)
            DELETE FROM admin_login_rate_limits
            WHERE limiter_key IN (
                SELECT limiter_key
                FROM admin_login_rate_limits
                WHERE updated_at < %s - make_interval(secs => %s)
                  AND (locked_until IS NULL OR locked_until < %s)
                ORDER BY updated_at ASC, limiter_key ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (now, retention_seconds, now, _TEST_BATCH_SIZE),
        )
        rows = cur.fetchall()
    plan_lines = [str(next(iter(row.values()))) for row in rows]

    plan_text = "\n".join(plan_lines).lower()
    assert "index scan using admin_login_rate_limits_updated_at_idx" in plan_text

    started = time.monotonic()
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    elapsed = time.monotonic() - started
    assert deleted == _TEST_BATCH_SIZE
    assert elapsed < _EXPLAIN_TIME_BUDGET_SECONDS
