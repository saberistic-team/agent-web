"""PostgreSQL integration tests for bounded admin login limiter cleanup (#332)."""

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
from app.config import get_settings
from app.migrations.runner import apply_migrations

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

_TEST_BATCH_SIZE = 10
_WINDOW_SECONDS = 60
_LOCKOUT_SECONDS = 60
_RETENTION_SECONDS = max(_WINDOW_SECONDS, _LOCKOUT_SECONDS) * 2
_EXPLAIN_ROW_COUNT = 2500
_EXPLAIN_TIME_BUDGET_SECONDS = 5.0

_CLEANUP_SELECT_SQL = """
    SELECT limiter_key
    FROM admin_login_rate_limits
    WHERE updated_at < %s - make_interval(secs => %s)
      AND (locked_until IS NULL OR locked_until < %s)
    ORDER BY updated_at ASC, limiter_key ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED
"""

pytestmark = [pytest.mark.integration]


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter cleanup tests")


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
            ON CONFLICT (limiter_key) DO UPDATE
            SET failure_count = EXCLUDED.failure_count,
                window_started_at = EXCLUDED.window_started_at,
                locked_until = EXCLUDED.locked_until,
                updated_at = EXCLUDED.updated_at
            """,
            (limiter_key, failure_count, updated_at, locked_until, updated_at),
        )
    conn.commit()


def _count_rows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _fetch_keys(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key")
        rows = cur.fetchall()
    return {str(row["limiter_key"]) for row in rows}


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


def _seed_cleanup_fixture(conn: psycopg.Connection, *, now: datetime) -> dict[str, set[str]]:
    """Seed >3 batches of expired rows plus protected rows."""
    expired_keys: set[str] = set()
    base = now - timedelta(seconds=_RETENTION_SECONDS + 60)
    for index in range(_TEST_BATCH_SIZE * 4 + 5):
        key = f"expired-{index:04d}"
        expired_keys.add(key)
        _insert_limiter_row(
            conn,
            limiter_key=key,
            updated_at=base + timedelta(seconds=index),
        )

    active_key = "active-recent"
    _insert_limiter_row(
        conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=30),
    )

    inside_retention_key = "inside-retention"
    _insert_limiter_row(
        conn,
        limiter_key=inside_retention_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS - 1),
    )

    locked_key = "currently-locked"
    _insert_limiter_row(
        conn,
        limiter_key=locked_key,
        updated_at=base,
        locked_until=now + timedelta(minutes=5),
    )

    return {
        "expired": expired_keys,
        "active": {active_key},
        "inside_retention": {inside_retention_key},
        "locked": {locked_key},
    }


def test_one_cleanup_call_respects_batch_size_and_deletes_oldest(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert 0 < deleted <= _TEST_BATCH_SIZE

    remaining = _fetch_keys(pg_conn)
    expected_deleted = {
        f"expired-{index:04d}" for index in range(_TEST_BATCH_SIZE)
    }
    assert expected_deleted.issubset(fixture["expired"] - remaining)
    assert fixture["active"].issubset(remaining)
    assert fixture["inside_retention"].issubset(remaining)
    assert fixture["locked"].issubset(remaining)


def test_repeated_cleanup_drains_only_eligible_rows(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)
    deleted_total = 0
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        deleted_total += deleted
        if deleted == 0:
            break
    assert deleted_total == len(fixture["expired"])
    remaining = _fetch_keys(pg_conn)
    assert remaining == fixture["active"] | fixture["inside_retention"] | fixture["locked"]


def test_retention_and_locked_until_boundaries_are_exact(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    eligible_key = "eligible-boundary"
    inside_key = "inside-boundary"
    locked_boundary_key = "locked-boundary"
    eligible_updated_at = now - timedelta(seconds=_RETENTION_SECONDS)
    inside_updated_at = now - timedelta(seconds=_RETENTION_SECONDS - 1)
    lock_expired_at = now - timedelta(microseconds=1)

    _insert_limiter_row(
        pg_conn,
        limiter_key=eligible_key,
        updated_at=eligible_updated_at - timedelta(microseconds=1),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=inside_key,
        updated_at=inside_updated_at,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=locked_boundary_key,
        updated_at=eligible_updated_at - timedelta(minutes=5),
        locked_until=now,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key="lock-expired",
        updated_at=eligible_updated_at - timedelta(minutes=5),
        locked_until=lock_expired_at,
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 2
    remaining = _fetch_keys(pg_conn)
    assert inside_key in remaining
    assert locked_boundary_key in remaining
    assert eligible_key not in remaining
    assert "lock-expired" not in remaining


def test_concurrent_cleanup_workers_delete_disjoint_batches(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    expired_count = _TEST_BATCH_SIZE * 3
    base = now - timedelta(seconds=_RETENTION_SECONDS + 120)
    for index in range(expired_count):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"concurrent-{index:04d}",
            updated_at=base + timedelta(seconds=index),
        )

    barrier = threading.Barrier(2, timeout=10)
    deleted_by_worker: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        with lock:
            deleted_by_worker.append(deleted)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(deleted_by_worker) == 2
    assert all(0 < count <= _TEST_BATCH_SIZE for count in deleted_by_worker)
    assert sum(deleted_by_worker) == _TEST_BATCH_SIZE * 2
    assert _count_rows(pg_conn) == expired_count - (_TEST_BATCH_SIZE * 2)


def test_concurrent_admission_update_prevents_active_row_deletion(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    active_key = admin_auth.build_source_rate_limit_key("203.0.113.88", get_settings())
    stale_key = "stale-for-cleanup"
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 60),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 60),
    )

    admission_started = threading.Event()
    admission_may_commit = threading.Event()
    cleanup_done = threading.Event()

    def slow_admission() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    """
                    SELECT failure_count, window_started_at, locked_until, updated_at
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
        assert deleted == 1

    admission_thread = threading.Thread(target=slow_admission)
    cleanup_thread = threading.Thread(target=cleanup_worker)
    admission_thread.start()
    cleanup_thread.start()
    cleanup_thread.join(timeout=15)
    assert cleanup_done.is_set()
    assert active_key in _fetch_keys(pg_conn)
    admission_may_commit.set()
    admission_thread.join(timeout=15)


def test_rollback_leaves_rows_claimable_by_later_cleanup(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    for index in range(_TEST_BATCH_SIZE):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"rollback-{index:04d}",
            updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 30),
        )

    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                _CLEANUP_SELECT_SQL,
                (now, _RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
            )
            locked = cur.fetchall()
            assert len(locked) == _TEST_BATCH_SIZE
        conn.rollback()

    assert _count_rows(pg_conn) == _TEST_BATCH_SIZE
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE
    assert _count_rows(pg_conn) == 0


def test_previous_secret_hmac_rows_deleted_without_secret_knowledge(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    previous_secret_key = "cafebabe" * 8
    _insert_limiter_row(
        pg_conn,
        limiter_key=previous_secret_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 120),
    )
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert previous_secret_key not in _fetch_keys(pg_conn)


def test_large_cardinality_cleanup_uses_updated_at_index(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(seconds=_RETENTION_SECONDS + 3600)
    for index in range(_EXPLAIN_ROW_COUNT):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"explain-{index:05d}",
            updated_at=base + timedelta(seconds=index),
        )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("ANALYZE admin_login_rate_limits")

    started = time.monotonic()
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            EXPLAIN (FORMAT TEXT)
            WITH selected AS (
                SELECT limiter_key
                FROM admin_login_rate_limits
                WHERE updated_at < %s - make_interval(secs => %s)
                  AND (locked_until IS NULL OR locked_until < %s)
                ORDER BY updated_at ASC, limiter_key ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM admin_login_rate_limits AS target
            USING selected
            WHERE target.limiter_key = selected.limiter_key
            """,
            (now, _RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
        )
        rows = cur.fetchall()
        plan_lines = [
            str(next(iter(row.values()))) if isinstance(row, dict) else str(row[0])
            for row in rows
        ]
    elapsed = time.monotonic() - started
    plan_text = "\n".join(plan_lines).lower()
    assert "index scan using admin_login_rate_limits_updated_at_idx" in plan_text
    assert "cte selected" in plan_text
    assert "limit" in plan_text
    assert elapsed < _EXPLAIN_TIME_BUDGET_SECONDS


def test_require_test_database_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.test_admin_login_rate_limit_cleanup_integration as mod
    from _pytest.outcomes import Failed

    monkeypatch.setattr(mod, "_REQUIRED", True)
    monkeypatch.setattr(mod, "_DATABASE_URL", "")
    with pytest.raises(Failed):
        mod._require_database_url()


@pytest.mark.unit
def test_cleanup_integration_module_requires_integration_marker() -> None:
    assert pytest.mark.integration in pytestmark


@pytest.mark.unit
def test_cleanup_integration_tests_are_marked_integration() -> None:
    import tests.test_admin_login_rate_limit_cleanup_integration as mod

    for name, obj in inspect.getmembers(mod):
        if not name.startswith("test_") or not callable(obj):
            continue
        if name in {
            "test_require_test_database_guard_fails_closed",
            "test_cleanup_integration_module_requires_integration_marker",
            "test_cleanup_integration_tests_are_marked_integration",
        }:
            continue
        marks = getattr(obj, "pytestmark", [])
        mark_names = [
            mark.name for mark in marks if isinstance(mark, pytest.MarkDecorator)
        ]
        assert "integration" in mark_names or "integration" in {
            mark.name for mark in mod.pytestmark
        }, name
