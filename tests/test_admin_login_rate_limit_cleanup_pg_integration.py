"""PostgreSQL integration tests for bounded admin login limiter cleanup."""

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

from app import db
from app.migrations.runner import apply_migrations

pytestmark = [pytest.mark.integration]

_TEST_BATCH_SIZE = 10
_WINDOW_SECONDS = 60
_LOCKOUT_SECONDS = 60
_RETENTION_SECONDS = max(_WINDOW_SECONDS, _LOCKOUT_SECONDS) * 2
_CLEANUP_EXPLAIN_ROW_COUNT = 800
_CLEANUP_EXPLAIN_TIME_BUDGET_SECONDS = 2.0

_CLEANUP_SELECT_SQL = """
SELECT limiter_key
FROM admin_login_rate_limits
WHERE updated_at < %s - make_interval(secs => %s)
  AND (locked_until IS NULL OR locked_until < %s)
ORDER BY updated_at, limiter_key
LIMIT %s
FOR UPDATE SKIP LOCKED
"""


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


def _list_limiter_keys(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key")
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


def _seed_cleanup_fixture(conn: psycopg.Connection, *, now: datetime) -> dict[str, str]:
    expired_count = _TEST_BATCH_SIZE * 4 + 1
    expired_keys: list[str] = []
    for index in range(expired_count):
        key = f"expired-{index:04d}"
        expired_keys.append(key)
        _insert_limiter_row(
            conn,
            limiter_key=key,
            updated_at=now - timedelta(seconds=_RETENTION_SECONDS + index + 1),
        )

    protected = {
        "active_recent": "active-recent",
        "active_locked": "active-locked",
        "recent_boundary": "recent-boundary",
        "lock_boundary": "lock-boundary",
        "previous_secret": "deadbeef" * 8,
    }
    _insert_limiter_row(
        conn,
        limiter_key=protected["active_recent"],
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS - 5),
    )
    _insert_limiter_row(
        conn,
        limiter_key=protected["active_locked"],
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 60),
        locked_until=now + timedelta(minutes=30),
    )
    _insert_limiter_row(
        conn,
        limiter_key=protected["recent_boundary"],
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS - 1),
    )
    _insert_limiter_row(
        conn,
        limiter_key=protected["lock_boundary"],
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 60),
        locked_until=now + timedelta(microseconds=1),
    )
    _insert_limiter_row(
        conn,
        limiter_key=protected["previous_secret"],
        updated_at=now - timedelta(hours=2),
    )
    return {
        "expired_keys": expired_keys,
        **protected,
    }


@pytest.mark.integration
def test_bounded_cleanup_deletes_at_most_batch_size(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == _TEST_BATCH_SIZE
    remaining = _list_limiter_keys(pg_conn)
    assert len(remaining) == len(fixture["expired_keys"]) - _TEST_BATCH_SIZE + 5


@pytest.mark.integration
def test_bounded_cleanup_selects_oldest_eligible_first(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)

    _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    remaining_expired = [
        key
        for key in _list_limiter_keys(pg_conn)
        if key.startswith("expired-")
    ]
    # `_seed_cleanup_fixture` assigns higher indices an older `updated_at`
    # (`RETENTION_SECONDS + index + 1` seconds back), so the oldest-first
    # batch removes the *highest*-indexed keys first. `previous_secret` is
    # seeded 2 hours back, older than every `expired-*` row, so it claims one
    # of the batch's slots before any `expired-*` row is removed.
    expired_slots = _TEST_BATCH_SIZE - 1
    expected_remaining = fixture["expired_keys"][:-expired_slots]
    assert remaining_expired == expected_remaining


@pytest.mark.integration
def test_repeated_bounded_cleanup_drains_eligible_rows(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    fixture = _seed_cleanup_fixture(pg_conn, now=now)

    total_deleted = 0
    while True:
        deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
        total_deleted += deleted
        if deleted == 0:
            break

    assert total_deleted == len(fixture["expired_keys"]) + 1
    remaining = set(_list_limiter_keys(pg_conn))
    assert remaining == {
        fixture["active_recent"],
        fixture["active_locked"],
        fixture["recent_boundary"],
        fixture["lock_boundary"],
    }


@pytest.mark.integration
def test_retention_and_locked_until_boundaries_are_exact(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(seconds=_RETENTION_SECONDS)

    inside_retention = "inside-retention"
    at_retention = "at-retention"
    outside_retention = "outside-retention"
    lock_now = "lock-now"
    lock_future = "lock-future"

    _insert_limiter_row(
        pg_conn,
        limiter_key=inside_retention,
        updated_at=cutoff + timedelta(microseconds=1),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=at_retention,
        updated_at=cutoff,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=outside_retention,
        updated_at=cutoff - timedelta(microseconds=1),
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=lock_now,
        updated_at=cutoff - timedelta(seconds=30),
        locked_until=now,
    )
    _insert_limiter_row(
        pg_conn,
        limiter_key=lock_future,
        updated_at=cutoff - timedelta(seconds=30),
        locked_until=now + timedelta(microseconds=1),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    # Only `outside_retention` is eligible: `at_retention` sits exactly on the
    # retention cutoff (strict `<` excludes it), and `lock_now`'s
    # `locked_until == now` is not yet "in the past" (strict `<` on
    # `locked_until` too), matching the `locked_until > now` "still locked"
    # check used elsewhere (e.g. admission checks).
    assert deleted == 1
    remaining = set(_list_limiter_keys(pg_conn))
    assert remaining == {inside_retention, at_retention, lock_now, lock_future}


@pytest.mark.integration
def test_concurrent_bounded_cleanup_claims_disjoint_rows(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    seeded_keys = []
    for index in range(_TEST_BATCH_SIZE * 4):
        key = f"concurrent-{index:04d}"
        seeded_keys.append(key)
        _insert_limiter_row(
            pg_conn,
            limiter_key=key,
            updated_at=now - timedelta(seconds=_RETENTION_SECONDS + index + 1),
        )

    barrier = threading.Barrier(2, timeout=10)
    deleted_counts: list[int] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = _cleanup(conn, now=now, batch_size=_TEST_BATCH_SIZE)
        with results_lock:
            deleted_counts.append(deleted)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert deleted_counts == [_TEST_BATCH_SIZE, _TEST_BATCH_SIZE]
    remaining = [
        key for key in _list_limiter_keys(pg_conn) if key.startswith("concurrent-")
    ]
    assert len(remaining) == len(seeded_keys) - (_TEST_BATCH_SIZE * 2)


@pytest.mark.integration
def test_concurrent_admission_prevents_active_row_deletion(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    active_key = "active-admission"
    _insert_limiter_row(
        pg_conn,
        limiter_key=active_key,
        updated_at=now - timedelta(seconds=5),
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
                    (active_key,),
                )
                row = cur.fetchone()
                assert row is not None
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


@pytest.mark.integration
def test_cleanup_rollback_leaves_rows_for_later_cleanup(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    stale_key = "rollback-stale"
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(seconds=_RETENTION_SECONDS + 30),
    )

    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                _CLEANUP_SELECT_SQL,
                (now, _RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
            )
            selected = cur.fetchall()
            assert len(selected) == 1
        conn.rollback()

    assert stale_key in _list_limiter_keys(pg_conn)
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert stale_key not in _list_limiter_keys(pg_conn)


@pytest.mark.integration
def test_previous_secret_rows_deleted_without_secret_knowledge(
    pg_conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    stale_key = "cafebabe" * 8
    _insert_limiter_row(
        pg_conn,
        limiter_key=stale_key,
        updated_at=now - timedelta(hours=2),
    )

    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    assert deleted == 1
    assert stale_key not in _list_limiter_keys(pg_conn)


@pytest.mark.integration
def test_cleanup_query_plan_uses_index_within_budget(pg_conn: psycopg.Connection) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    for index in range(_CLEANUP_EXPLAIN_ROW_COUNT):
        _insert_limiter_row(
            pg_conn,
            limiter_key=f"plan-{index:05d}",
            updated_at=now - timedelta(seconds=_RETENTION_SECONDS + index + 1),
        )
    pg_conn.execute("ANALYZE admin_login_rate_limits")
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            f"EXPLAIN (FORMAT TEXT) {_CLEANUP_SELECT_SQL}",
            (now, _RETENTION_SECONDS, now, _TEST_BATCH_SIZE),
        )
        plan_lines = [str(next(iter(row.values()))) for row in cur.fetchall()]
    plan_text = "\n".join(plan_lines).lower()
    assert "index scan" in plan_text or "index only scan" in plan_text
    assert "admin_login_rate_limits_cleanup_idx" in plan_text or (
        "admin_login_rate_limits_updated_at_idx" in plan_text
    )

    started = time.monotonic()
    deleted = _cleanup(pg_conn, now=now, batch_size=_TEST_BATCH_SIZE)
    elapsed = time.monotonic() - started
    assert deleted == _TEST_BATCH_SIZE
    assert elapsed < _CLEANUP_EXPLAIN_TIME_BUDGET_SECONDS


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
    import tests.test_admin_login_rate_limit_cleanup_pg_integration as cleanup_integration

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
