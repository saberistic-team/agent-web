"""PostgreSQL integration tests for bounded admin login rate-limit cleanup."""

from __future__ import annotations

import inspect
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

_TEST_BATCH_SIZE = 5
_TEST_WINDOW_SECONDS = 60
_TEST_LOCKOUT_SECONDS = 60
_TEST_RETENTION_SECONDS = max(_TEST_WINDOW_SECONDS, _TEST_LOCKOUT_SECONDS) * 2
_EXPLAIN_ROW_COUNT = 800
_EXPLAIN_CLEANUP_BUDGET_SECONDS = 0.5

pytestmark = [pytest.mark.integration]


def _require_database_url() -> str:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    if database_url:
        return database_url
    if required:
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


def _retention_cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=_TEST_RETENTION_SECONDS)


def _insert_limiter_row(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    updated_at: datetime,
    locked_until: datetime | None = None,
    window_started_at: datetime | None = None,
) -> None:
    started = window_started_at or updated_at
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, %s, %s)
            """,
            (limiter_key, started, locked_until, updated_at),
        )
    conn.commit()


def _cleanup(
    conn: psycopg.Connection,
    *,
    now: datetime,
    batch_size: int = _TEST_BATCH_SIZE,
) -> int:
    return db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=_TEST_WINDOW_SECONDS,
        lockout_seconds=_TEST_LOCKOUT_SECONDS,
        batch_size=batch_size,
    )


def _list_limiter_keys(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key"
        )
        return [str(row["limiter_key"]) for row in cur.fetchall()]


def _seed_expired_rows(
    conn: psycopg.Connection,
    *,
    now: datetime,
    count: int,
    key_prefix: str = "expired",
) -> list[str]:
    cutoff = _retention_cutoff(now)
    keys: list[str] = []
    for index in range(count):
        limiter_key = f"{key_prefix}-{index:04d}"
        updated_at = cutoff - timedelta(seconds=count - index)
        _insert_limiter_row(conn, limiter_key=limiter_key, updated_at=updated_at)
        keys.append(limiter_key)
    return keys


def test_bounded_cleanup_deletes_oldest_eligible_rows_first(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    expired_count = _TEST_BATCH_SIZE * 4 + 2
    oldest_keys = _seed_expired_rows(pg_conn, now=now, count=expired_count)

    recent_key = "active-recent"
    _insert_limiter_row(
        pg_conn,
        limiter_key=recent_key,
        updated_at=now - timedelta(seconds=30),
    )
    locked_key = "active-locked"
    _insert_limiter_row(
        pg_conn,
        limiter_key=locked_key,
        updated_at=_retention_cutoff(now) - timedelta(hours=1),
        locked_until=now + timedelta(minutes=10),
    )
    inside_retention_key = "inside-retention"
    _insert_limiter_row(
        pg_conn,
        limiter_key=inside_retention_key,
        updated_at=_retention_cutoff(now) + timedelta(seconds=1),
    )

    first_deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert first_deleted == _TEST_BATCH_SIZE
    remaining = set(_list_limiter_keys(pg_conn))
    assert remaining & set(oldest_keys[:_TEST_BATCH_SIZE]) == set()
    assert {recent_key, locked_key, inside_retention_key}.issubset(remaining)

    total_deleted = first_deleted
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        total_deleted += deleted
        if deleted == 0:
            break

    assert total_deleted == expired_count
    assert set(_list_limiter_keys(pg_conn)) == {
        recent_key,
        locked_key,
        inside_retention_key,
    }


def test_retention_and_locked_until_boundaries_are_deterministic(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    cutoff = _retention_cutoff(now)

    eligible_key = "eligible-boundary"
    _insert_limiter_row(
        pg_conn,
        limiter_key=eligible_key,
        updated_at=cutoff - timedelta(seconds=1),
        locked_until=now - timedelta(seconds=1),
    )
    too_recent_key = "too-recent"
    _insert_limiter_row(
        pg_conn,
        limiter_key=too_recent_key,
        updated_at=cutoff + timedelta(seconds=1),
    )
    still_locked_key = "still-locked"
    _insert_limiter_row(
        pg_conn,
        limiter_key=still_locked_key,
        updated_at=cutoff - timedelta(hours=1),
        locked_until=now + timedelta(seconds=1),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert set(_list_limiter_keys(pg_conn)) == {too_recent_key, still_locked_key}


def test_concurrent_cleanup_workers_delete_disjoint_batches(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    expired_keys = _seed_expired_rows(pg_conn, now=now, count=12, key_prefix="concurrent")
    barrier = threading.Barrier(2)
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

    assert len(deleted_counts) == 2
    assert all(0 < count <= _TEST_BATCH_SIZE for count in deleted_counts)
    assert sum(deleted_counts) == min(len(expired_keys), _TEST_BATCH_SIZE * 2)
    remaining_expired = [
        key for key in _list_limiter_keys(pg_conn) if key in set(expired_keys)
    ]
    assert len(remaining_expired) == len(expired_keys) - sum(deleted_counts)


def test_concurrent_admission_update_prevents_active_row_deletion(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    active_key = "active-during-cleanup"
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=30),
    )
    _seed_expired_rows(pg_conn, now=now, count=6, key_prefix="stale")

    admission_started = threading.Event()
    admission_may_commit = threading.Event()
    cleanup_done = threading.Event()

    def slow_admission() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    """
                    SELECT failure_count
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
        assert active_key in _list_limiter_keys(pg_conn)
        assert deleted <= _TEST_BATCH_SIZE

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
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    expired_keys = _seed_expired_rows(pg_conn, now=now, count=3, key_prefix="rollback")
    cutoff = _retention_cutoff(now)

    with pg_conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(
            """
            DELETE FROM admin_login_rate_limits
            WHERE limiter_key IN (
                SELECT limiter_key
                FROM admin_login_rate_limits
                WHERE updated_at < %s
                  AND (locked_until IS NULL OR locked_until < %s)
                ORDER BY updated_at, limiter_key
                LIMIT %s
                FOR UPDATE
            )
            """,
            (cutoff, now, _TEST_BATCH_SIZE),
        )
        pg_conn.rollback()

    assert set(_list_limiter_keys(pg_conn)) == set(expired_keys)
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == len(expired_keys)
    assert _list_limiter_keys(pg_conn) == []


def test_previous_secret_rows_deleted_by_age_without_secret_knowledge(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    stale_previous_key = "cafebabe" * 8
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_previous_key,
        updated_at=_retention_cutoff(now) - timedelta(hours=1),
    )
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert _list_limiter_keys(pg_conn) == []


def test_large_cardinality_cleanup_uses_updated_at_index(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    _seed_expired_rows(pg_conn, now=now, count=_EXPLAIN_ROW_COUNT, key_prefix="explain")
    cutoff = _retention_cutoff(now)

    with pg_conn.cursor() as cur:
        cur.execute("ANALYZE admin_login_rate_limits")
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            """
            EXPLAIN (FORMAT TEXT)
            SELECT limiter_key
            FROM admin_login_rate_limits
            WHERE updated_at < %s
              AND (locked_until IS NULL OR locked_until < %s)
            ORDER BY updated_at, limiter_key
            LIMIT %s
            """,
            (cutoff, now, admin_auth.LOGIN_RATE_LIMIT_CLEANUP_BATCH_SIZE),
        )
        plan_lines = [str(row["QUERY PLAN"]) for row in cur.fetchall()]

    plan_text = "\n".join(plan_lines).lower()
    assert "admin_login_rate_limits_updated_at_idx" in plan_text

    started = time.monotonic()
    deleted = _cleanup(
        pg_conn,
        now=now,
        batch_size=admin_auth.LOGIN_RATE_LIMIT_CLEANUP_BATCH_SIZE,
    )
    elapsed = time.monotonic() - started
    assert deleted == admin_auth.LOGIN_RATE_LIMIT_CLEANUP_BATCH_SIZE
    assert elapsed < _EXPLAIN_CLEANUP_BUDGET_SECONDS


def test_require_database_url_fails_closed_when_ci_requires_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_TEST_DATABASE", "1")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(
        BaseException, match="REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset"
    ):
        _require_database_url()


def test_cleanup_integration_module_is_collected_as_integration_tests() -> None:
    import tests.test_admin_login_rate_limit_cleanup_integration as cleanup_integration

    for name, obj in inspect.getmembers(cleanup_integration):
        if not name.startswith("test_") or not callable(obj):
            continue
        marks = getattr(obj, "pytestmark", [])
        mark_names = [
            mark.name for mark in marks if isinstance(mark, pytest.MarkDecorator)
        ]
        assert "integration" in mark_names or "integration" in {
            mark.name for mark in cleanup_integration.pytestmark
        }, name
