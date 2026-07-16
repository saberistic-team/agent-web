"""PostgreSQL integration tests for bounded admin login limiter cleanup (#332)."""

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
_LARGE_CARDINALITY_ROWS = 10_000
_EXPLAIN_TIME_BUDGET_SECONDS = 2.0

pytestmark = [pytest.mark.integration]


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter cleanup tests")


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


def _retention_seconds(*, window_seconds: int, lockout_seconds: int) -> int:
    return max(window_seconds, lockout_seconds) * 2


def _insert_limiter_row(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    updated_at: datetime,
    locked_until: datetime | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, %s, %s)
            """,
            (limiter_key, updated_at, locked_until, updated_at),
        )
    conn.commit()


def _count_rows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _list_keys(conn: psycopg.Connection) -> list[str]:
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
    window_seconds: int = 60,
    lockout_seconds: int = 60,
    batch_size: int = _TEST_BATCH_SIZE,
) -> int:
    return db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
        batch_size=batch_size,
    )


def _seed_cleanup_fixture(
    conn: psycopg.Connection,
    *,
    now: datetime,
    window_seconds: int = 60,
    lockout_seconds: int = 60,
) -> dict[str, list[str]]:
    retention = _retention_seconds(
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
    )
    stale_base = now - timedelta(seconds=retention + 60)
    expired_keys: list[str] = []
    for index in range(_TEST_BATCH_SIZE * 3 + 1):
        key = f"expired-{index:04d}"
        expired_keys.append(key)
        _insert_limiter_row(
            conn,
            limiter_key=key,
            updated_at=stale_base + timedelta(seconds=index),
        )

    active_key = "active-recent"
    _insert_limiter_row(
        conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=30),
    )

    locked_key = "active-locked"
    _insert_limiter_row(
        conn,
        limiter_key=locked_key,
        updated_at=stale_base,
        locked_until=now + timedelta(minutes=5),
    )

    boundary_key = "retention-boundary"
    boundary_updated_at = now - timedelta(seconds=retention)
    _insert_limiter_row(
        conn,
        limiter_key=boundary_key,
        updated_at=boundary_updated_at,
    )

    previous_secret_key = "deadbeef" * 8
    _insert_limiter_row(
        conn,
        limiter_key=previous_secret_key,
        updated_at=stale_base - timedelta(hours=1),
    )

    return {
        "expired": expired_keys,
        "active": [active_key],
        "locked": [locked_key],
        "boundary": [boundary_key],
        "previous_secret": [previous_secret_key],
    }


@pytest.mark.integration
def test_bounded_cleanup_deletes_oldest_eligible_rows_first(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    seeded = _seed_cleanup_fixture(pg_conn, now=now)

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE

    remaining_expired = [
        key for key in _list_keys(pg_conn) if key.startswith("expired-")
    ]
    assert seeded["previous_secret"][0] not in _list_keys(pg_conn)
    assert remaining_expired == seeded["expired"][_TEST_BATCH_SIZE - 1 :]
    assert seeded["active"][0] in _list_keys(pg_conn)
    assert seeded["locked"][0] in _list_keys(pg_conn)
    assert seeded["boundary"][0] in _list_keys(pg_conn)


@pytest.mark.integration
def test_repeated_cleanup_drains_only_eligible_rows(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    seeded = _seed_cleanup_fixture(pg_conn, now=now)
    total_deleted = 0
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        total_deleted += deleted
        if deleted < _TEST_BATCH_SIZE:
            break

    expected_deleted = len(seeded["expired"]) + len(seeded["previous_secret"])
    assert total_deleted == expected_deleted
    assert _list_keys(pg_conn) == seeded["active"] + seeded["locked"] + seeded["boundary"]


@pytest.mark.integration
def test_retention_and_lockout_boundaries_are_exact(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    window_seconds = 60
    lockout_seconds = 60
    retention = _retention_seconds(
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
    )
    stale_base = now - timedelta(seconds=retention + 10)

    just_outside = "just-outside-retention"
    _insert_limiter_row(
        pg_conn,
        limiter_key=just_outside,
        updated_at=now - timedelta(seconds=retention - 1),
    )
    just_inside = "just-inside-retention"
    _insert_limiter_row(
        pg_conn,
        limiter_key=just_inside,
        updated_at=now - timedelta(seconds=retention + 1),
    )
    expired_lockout = "expired-lockout"
    _insert_limiter_row(
        pg_conn,
        limiter_key=expired_lockout,
        updated_at=stale_base,
        locked_until=now - timedelta(seconds=1),
    )
    active_lockout = "active-lockout"
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_lockout,
        updated_at=stale_base,
        locked_until=now,
    )

    deleted = _cleanup(
        pg_conn,
        now=now,
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
        batch_size=_TEST_BATCH_SIZE,
    )
    assert deleted == 2
    assert _list_keys(pg_conn) == [just_outside, active_lockout]


@pytest.mark.integration
def test_concurrent_cleanup_workers_delete_disjoint_batches(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    retention = _retention_seconds(window_seconds=60, lockout_seconds=60)
    stale_base = now - timedelta(seconds=retention + 60)
    for index in range(_TEST_BATCH_SIZE * 2 + 3):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"parallel-{index:04d}",
            updated_at=stale_base + timedelta(seconds=index),
        )

    barrier = threading.Barrier(2)
    deleted_counts: list[int] = []
    counts_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        with counts_lock:
            deleted_counts.append(deleted)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(deleted_counts) == 2
    assert all(0 < count <= _TEST_BATCH_SIZE for count in deleted_counts)
    assert sum(deleted_counts) == _TEST_BATCH_SIZE * 2 + 3
    assert _count_rows(pg_conn) == 0


@pytest.mark.integration
def test_concurrent_admission_prevents_deleting_refreshed_row(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    retention = _retention_seconds(window_seconds=60, lockout_seconds=60)
    stale_key = "stale-being-refreshed"
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(seconds=retention + 60),
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
                    SELECT limiter_key
                    FROM admin_login_rate_limits
                    WHERE limiter_key = %s
                    FOR UPDATE
                    """,
                    (stale_key,),
                )
                cur.execute(
                    """
                    UPDATE admin_login_rate_limits
                    SET failure_count = failure_count + 1,
                        updated_at = %s
                    WHERE limiter_key = %s
                    """,
                    (now, stale_key),
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
    assert stale_key in _list_keys(pg_conn)
    admission_may_commit.set()
    admission_thread.join(timeout=15)


@pytest.mark.integration
def test_rollback_leaves_rows_claimable_by_later_cleanup(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    retention = _retention_seconds(window_seconds=60, lockout_seconds=60)
    stale_base = now - timedelta(seconds=retention + 30)
    for index in range(3):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"rollback-{index}",
            updated_at=stale_base + timedelta(seconds=index),
        )

    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                """
                WITH eligible AS (
                    SELECT limiter_key
                    FROM admin_login_rate_limits
                    WHERE updated_at < %s - make_interval(secs => %s)
                      AND (locked_until IS NULL OR locked_until < %s)
                    ORDER BY updated_at ASC, limiter_key ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                DELETE FROM admin_login_rate_limits AS limits
                WHERE limits.limiter_key IN (SELECT limiter_key FROM eligible)
                """,
                (now, retention, now, _TEST_BATCH_SIZE),
            )
            assert cur.rowcount == 3
        conn.rollback()

    assert _count_rows(pg_conn) == 3
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 3
    assert _count_rows(pg_conn) == 0


@pytest.mark.integration
def test_previous_secret_rows_deleted_without_secret_knowledge(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    stale_key = "cafebabe" * 8
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(hours=3),
    )

    deleted = _cleanup(
        pg_conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
        batch_size=_TEST_BATCH_SIZE,
    )
    assert deleted == 1
    assert _count_rows(pg_conn) == 0


@pytest.mark.integration
def test_large_cardinality_cleanup_uses_updated_at_index(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    retention = _retention_seconds(window_seconds=60, lockout_seconds=60)
    stale_base = now - timedelta(seconds=retention + 3600)
    rows = [
        (
            f"bulk-{index:05d}",
            1,
            stale_base + timedelta(seconds=index),
            None,
            stale_base + timedelta(seconds=index),
        )
        for index in range(_LARGE_CARDINALITY_ROWS)
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            rows,
        )
    pg_conn.commit()

    explain_sql = """
        EXPLAIN (FORMAT TEXT)
        WITH eligible AS (
            SELECT limiter_key
            FROM admin_login_rate_limits
            WHERE updated_at < %s - make_interval(secs => %s)
              AND (locked_until IS NULL OR locked_until < %s)
            ORDER BY updated_at ASC, limiter_key ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM admin_login_rate_limits AS limits
        WHERE limits.limiter_key IN (SELECT limiter_key FROM eligible)
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            explain_sql,
            (now, retention, now, admin_auth.ADMIN_LOGIN_LIMITER_CLEANUP_BATCH_SIZE),
        )
        plan_lines = [str(row[0]) for row in cur.fetchall()]
    plan_text = "\n".join(plan_lines).lower()
    assert "admin_login_rate_limits_updated_at_idx" in plan_text

    started = time.monotonic()
    deleted = _cleanup(
        pg_conn,
        now=now,
        batch_size=admin_auth.ADMIN_LOGIN_LIMITER_CLEANUP_BATCH_SIZE,
    )
    elapsed = time.monotonic() - started
    assert deleted == admin_auth.ADMIN_LOGIN_LIMITER_CLEANUP_BATCH_SIZE
    assert elapsed < _EXPLAIN_TIME_BUDGET_SECONDS


@pytest.mark.unit
def test_require_test_database_guard_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_admin_login_rate_limit_cleanup_integration as mod
    from _pytest.outcomes import Failed

    monkeypatch.setattr(mod, "_REQUIRED", True)
    monkeypatch.setattr(mod, "_DATABASE_URL", "")
    with pytest.raises(Failed):
        mod._require_database_url()


@pytest.mark.unit
def test_cleanup_module_requires_integration_marker() -> None:
    assert pytest.mark.integration in pytestmark
