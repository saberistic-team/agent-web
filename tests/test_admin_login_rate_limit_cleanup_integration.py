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
_TEST_WINDOW_SECONDS = 60
_TEST_LOCKOUT_SECONDS = 60
_TEST_RETENTION_SECONDS = max(_TEST_WINDOW_SECONDS, _TEST_LOCKOUT_SECONDS) * 2
_EXPLAIN_ROW_COUNT = 600
_EXPLAIN_TIME_BUDGET_SECONDS = 2.0

_CLEANUP_SELECT_SQL = """
SELECT limiter_key
FROM admin_login_rate_limits
WHERE updated_at < %s - make_interval(secs => %s)
  AND (locked_until IS NULL OR locked_until < %s)
ORDER BY updated_at, limiter_key
FOR UPDATE SKIP LOCKED
LIMIT %s
"""


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
            (
                limiter_key,
                failure_count,
                updated_at,
                locked_until,
                updated_at,
            ),
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


def _eligible_keys(conn: psycopg.Connection, *, now: datetime) -> list[str]:
    cutoff = now - timedelta(seconds=_TEST_RETENTION_SECONDS)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT limiter_key
            FROM admin_login_rate_limits
            WHERE updated_at < %s
              AND (locked_until IS NULL OR locked_until < %s)
            ORDER BY updated_at, limiter_key
            """,
            (cutoff, now),
        )
        rows = cur.fetchall()
    return [str(row["limiter_key"]) for row in rows]


def _all_keys(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key"
        )
        rows = cur.fetchall()
    return [str(row["limiter_key"]) for row in rows]


def _seed_cleanup_fixture(conn: psycopg.Connection, *, now: datetime) -> dict[str, list[str]]:
    retention = _TEST_RETENTION_SECONDS
    stale_base = now - timedelta(seconds=retention + 60)
    expired_keys: list[str] = []
    for index in range(35):
        key = f"expired-{index:03d}"
        updated_at = stale_base - timedelta(seconds=index)
        _insert_limiter_row(conn, limiter_key=key, updated_at=updated_at)
        expired_keys.append(key)

    active_key = "active-recent"
    _insert_limiter_row(
        conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=5),
    )

    boundary_key = "boundary-inside-retention"
    _insert_limiter_row(
        conn,
        limiter_key=boundary_key,
        updated_at=now - timedelta(seconds=retention - 1),
    )

    locked_key = "active-lockout"
    _insert_limiter_row(
        conn,
        limiter_key=locked_key,
        updated_at=stale_base,
        locked_until=now + timedelta(minutes=10),
        failure_count=5,
    )

    expired_unlocked_key = "expired-lockout-elapsed"
    _insert_limiter_row(
        conn,
        limiter_key=expired_unlocked_key,
        updated_at=stale_base - timedelta(hours=1),
        locked_until=now - timedelta(seconds=1),
        failure_count=5,
    )
    expired_keys.append(expired_unlocked_key)

    return {
        "expired": expired_keys,
        "protected": [active_key, boundary_key, locked_key],
    }


@pytest.mark.integration
def test_bounded_cleanup_deletes_oldest_eligible_first(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)
    eligible_before = _eligible_keys(pg_conn, now=now)

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE

    remaining_eligible = _eligible_keys(pg_conn, now=now)
    assert remaining_eligible == eligible_before[_TEST_BATCH_SIZE:]
    assert set(_all_keys(pg_conn)) == set(fixture["protected"] + remaining_eligible)


@pytest.mark.integration
def test_repeated_bounded_cleanup_drains_eligible_rows_only(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)
    total_deleted = 0
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        total_deleted += deleted
        if deleted == 0:
            break

    assert total_deleted == len(fixture["expired"])
    assert _eligible_keys(pg_conn, now=now) == []
    assert set(_all_keys(pg_conn)) == set(fixture["protected"])


@pytest.mark.integration
def test_retention_and_lockout_boundaries_are_exact(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    retention = _TEST_RETENTION_SECONDS

    inside_retention = "inside-retention"
    _insert_limiter_row(
        pg_conn,
        limiter_key=inside_retention,
        updated_at=now - timedelta(seconds=retention - 1),
    )

    at_retention = "at-retention-boundary"
    _insert_limiter_row(
        pg_conn,
        limiter_key=at_retention,
        updated_at=now - timedelta(seconds=retention),
    )

    eligible = "eligible-past-retention"
    _insert_limiter_row(
        pg_conn,
        limiter_key=eligible,
        updated_at=now - timedelta(seconds=retention + 1),
    )

    lock_at_boundary = "lock-expires-at-now"
    _insert_limiter_row(
        pg_conn,
        limiter_key=lock_at_boundary,
        updated_at=now - timedelta(seconds=retention + 10),
        locked_until=now,
    )

    lock_before_now = "lock-expired"
    _insert_limiter_row(
        pg_conn,
        limiter_key=lock_before_now,
        updated_at=now - timedelta(seconds=retention + 10),
        locked_until=now - timedelta(microseconds=1),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 2
    assert set(_all_keys(pg_conn)) == {
        inside_retention,
        at_retention,
        lock_at_boundary,
    }


@pytest.mark.integration
def test_concurrent_cleanup_workers_delete_disjoint_batches(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    stale_base = now - timedelta(seconds=_TEST_RETENTION_SECONDS + 120)
    for index in range(24):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"concurrent-{index:02d}",
            updated_at=stale_base - timedelta(seconds=index),
        )

    barrier = threading.Barrier(2, timeout=10)
    deleted_by_worker: dict[str, int] = {}
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        with lock:
            deleted_by_worker[name] = deleted

    threads = [
        threading.Thread(target=worker, args=("a",)),
        threading.Thread(target=worker, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert deleted_by_worker["a"] <= _TEST_BATCH_SIZE
    assert deleted_by_worker["b"] <= _TEST_BATCH_SIZE
    total_deleted = deleted_by_worker["a"] + deleted_by_worker["b"]
    assert 10 <= total_deleted <= 20
    assert len(_eligible_keys(pg_conn, now=now)) == 24 - total_deleted


@pytest.mark.integration
def test_concurrent_admission_update_prevents_active_row_deletion(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    stale_base = now - timedelta(seconds=_TEST_RETENTION_SECONDS + 60)
    protected_key = "protected-by-admission"
    _insert_limiter_row(
        pg_conn,
        limiter_key=protected_key,
        updated_at=stale_base,
    )
    for index in range(12):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"stale-{index:02d}",
            updated_at=stale_base - timedelta(seconds=index + 1),
        )

    admission_done = threading.Event()
    cleanup_done = threading.Event()

    def refresh_active_row() -> None:
        with _connect(database_url) as conn:
            db.try_admit_admin_login(
                conn,
                limiter_keys=(protected_key,),
                now=now,
                rate_limit=5,
                window_seconds=_TEST_WINDOW_SECONDS,
                lockout_seconds=_TEST_LOCKOUT_SECONDS,
            )
        admission_done.set()

    def cleanup_worker() -> None:
        admission_done.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        cleanup_done.set()
        assert deleted == _TEST_BATCH_SIZE

    admission_thread = threading.Thread(target=refresh_active_row)
    cleanup_thread = threading.Thread(target=cleanup_worker)
    admission_thread.start()
    admission_thread.join(timeout=15)
    assert admission_done.is_set()
    cleanup_thread.start()
    cleanup_thread.join(timeout=15)

    assert cleanup_done.is_set()
    assert admission_done.is_set()
    assert protected_key in _all_keys(pg_conn)


@pytest.mark.integration
def test_rollback_leaves_rows_claimable_by_later_cleanup(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    stale_base = now - timedelta(seconds=_TEST_RETENTION_SECONDS + 30)
    keys = []
    for index in range(5):
        key = f"rollback-{index}"
        _insert_limiter_row(
            pg_conn,
            limiter_key=key,
            updated_at=stale_base - timedelta(seconds=index),
        )
        keys.append(key)

    with pg_conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(
            _CLEANUP_SELECT_SQL,
            (now, _TEST_RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
        )
        selected = [str(row["limiter_key"]) for row in cur.fetchall()]
        assert len(selected) == 5
        pg_conn.rollback()

    assert set(_all_keys(pg_conn)) == set(keys)
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 5
    assert _all_keys(pg_conn) == []


@pytest.mark.integration
def test_previous_secret_hmac_rows_deleted_without_secret_knowledge(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    stale_key = "deadbeef" * 8
    fresh_key = "cafebabe" * 8
    stale_updated = now - timedelta(seconds=_TEST_RETENTION_SECONDS + 120)
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=stale_updated,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=fresh_key,
        updated_at=now - timedelta(seconds=5),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert _all_keys(pg_conn) == [fresh_key]


@pytest.mark.integration
def test_large_cardinality_cleanup_uses_updated_at_index(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    stale_base = now - timedelta(seconds=_TEST_RETENTION_SECONDS + 3600)
    for index in range(_EXPLAIN_ROW_COUNT):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"plan-{index:04d}",
            updated_at=stale_base - timedelta(seconds=index),
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            f"EXPLAIN (FORMAT TEXT) {_CLEANUP_SELECT_SQL}",
            (now, _TEST_RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
        )
        rows = cur.fetchall()
    plan_lines = [next(iter(row.values())) for row in rows]
    plan_text = "\n".join(plan_lines).lower()
    # The composite `(updated_at, limiter_key)` index added for ordered batch
    # selection is preferred by the planner over the older single-column
    # index, but either satisfies "not a sequential scan".
    assert "admin_login_rate_limits_cleanup_idx" in plan_text or (
        "admin_login_rate_limits_updated_at_idx" in plan_text
    )
    assert "seq scan on admin_login_rate_limits" not in plan_text

    started = time.perf_counter()
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    elapsed = time.perf_counter() - started
    assert deleted == _TEST_BATCH_SIZE
    assert elapsed < _EXPLAIN_TIME_BUDGET_SECONDS


@pytest.mark.integration
def test_require_test_database_cannot_skip_module(database_url: str) -> None:
    assert database_url


@pytest.mark.integration
def test_cleanup_failure_does_not_affect_subsequent_admission(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    source_key = "source-key-for-cleanup-failure"
    _insert_limiter_row(
        pg_conn,
        limiter_key=source_key,
        updated_at=now,
    )

    def failing_cleanup(*args: object, **kwargs: object) -> int:
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(
        admin_auth.db,
        "cleanup_expired_admin_login_rate_limits",
        failing_cleanup,
    )

    from unittest.mock import patch

    from starlette.requests import Request

    from app.config import get_settings

    settings = get_settings()
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
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)

    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = pg_conn
        db_conn.return_value.__exit__.return_value = None
        result = admin_auth.try_admit_login_attempt(
            request,
            settings,
            username="operator",
        )

    assert result.admitted
    assert not result.store_unavailable
    assert source_key in _all_keys(pg_conn)
