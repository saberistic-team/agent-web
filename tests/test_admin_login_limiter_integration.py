"""Real-Postgres tests for atomic admin login limiter admission (#215)."""

from __future__ import annotations

import concurrent.futures
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from app import db
from app.migrations.runner import apply_migrations

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


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


def _reset_rate_limits(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM admin_login_rate_limits")
    conn.commit()


@pytest.fixture()
def postgres_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with _connect(database_url) as conn:
        apply_migrations(conn)
        _reset_rate_limits(conn)
        yield conn
        _reset_rate_limits(conn)


@pytest.mark.integration
def test_admit_admin_login_attempt_is_atomic_under_concurrency(
    database_url: str,
    postgres_conn: psycopg.Connection,
) -> None:
    now = datetime.now(timezone.utc)
    limiter_key = "integration-concurrent-key"
    barrier = threading.Barrier(8)
    admitted_total = {"count": 0}
    lock = threading.Lock()

    def run_worker() -> None:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            barrier.wait(timeout=5)
            admitted, _ = db.admit_admin_login_attempt(
                conn,
                limiter_keys=(limiter_key,),
                now=now,
                rate_limit=3,
                window_seconds=900,
                lockout_seconds=900,
            )
            if admitted:
                with lock:
                    admitted_total["count"] += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_worker) for _ in range(8)]
        for future in futures:
            future.result(timeout=10)

    assert admitted_total["count"] == 3

    with postgres_conn.cursor() as cur:
        cur.execute(
            """
            SELECT failure_count, locked_until
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            """,
            (limiter_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["failure_count"]) == 3
    assert row["locked_until"] is not None


@pytest.mark.integration
def test_admit_admin_login_attempt_resets_after_window(postgres_conn: psycopg.Connection) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    limiter_key = "integration-window-key"

    for _ in range(2):
        admitted, _ = db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=(limiter_key,),
            now=now,
            rate_limit=2,
            window_seconds=60,
            lockout_seconds=60,
        )
        assert admitted

    blocked, _ = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=(limiter_key,),
        now=now,
        rate_limit=2,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert not blocked

    after_window = now + timedelta(seconds=61)
    admitted, _ = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=(limiter_key,),
        now=after_window,
        rate_limit=2,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert admitted


@pytest.mark.integration
def test_admit_admin_login_attempt_rolls_back_partial_bucket_updates(
    postgres_conn: psycopg.Connection,
) -> None:
    now = datetime.now(timezone.utc)
    first_key = "integration-rollback-a"
    second_key = "integration-rollback-b"

    db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=(second_key,),
        now=now,
        rate_limit=1,
        window_seconds=900,
        lockout_seconds=900,
    )

    admitted, _ = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=(first_key, second_key),
        now=now,
        rate_limit=1,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admitted

    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (first_key,),
        )
        assert cur.fetchone() is None
