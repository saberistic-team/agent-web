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
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.fixture(scope="module")
def limiter_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    return get_settings()


@pytest.fixture(scope="module", autouse=True)
def _limiter_env() -> None:
    os.environ["ADMIN_LOGIN_LIMITER_SECRET"] = TEST_LIMITER_SECRET
    os.environ.pop("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", None)


@pytest.fixture(scope="module")
def limiter_settings() -> Settings:
    return get_settings()


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
def test_username_rotation_shares_source_bucket(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.10", limiter_settings)

    for index in range(5):
        user_key = admin_auth.build_rate_limit_key(
            f"user-{index}",
            "203.0.113.10",
            limiter_settings,
        )
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
    limiter_settings: Settings,
) -> None:
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.20", limiter_settings)
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
    limiter_settings: Settings,
) -> None:
    account_key = admin_auth.build_account_rate_limit_key("operator", limiter_settings)
    now = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    for index in range(5):
        source_key = admin_auth.build_source_rate_limit_key(
            f"203.0.113.{index + 1}",
            limiter_settings,
        )
        admission = _admit(
            pg_conn,
            keys=(source_key, account_key),
            now=now + timedelta(seconds=index),
            rate_limit=5,
        )
        assert admission.admitted

    blocked_source = admin_auth.build_source_rate_limit_key("203.0.113.99", limiter_settings)
    blocked = _admit(
        pg_conn,
        keys=(blocked_source, account_key),
        now=now + timedelta(seconds=20),
        rate_limit=5,
    )
    assert not blocked.admitted
    assert blocked.already_locked


@pytest.mark.integration
def test_window_boundary_resets_failure_count(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.44", limiter_settings)
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
def test_expired_lockout_allows_new_admissions(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.55", limiter_settings)
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
def test_cleanup_removes_stale_unlocked_rows(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.66", limiter_settings)
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
def test_persisted_limiter_rows_use_keyed_hmac_identifiers(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    import hashlib

    source = "203.0.113.88"
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    source_key = admin_auth.build_source_rate_limit_key(source, limiter_settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert source_key != plain
    _admit(pg_conn, keys=(source_key,), now=now, rate_limit=5)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert len(row["limiter_key"]) == 64


@pytest.mark.integration
def test_rotation_admits_against_previous_secret_rows(
    pg_conn: psycopg.Connection,
    limiter_settings: Settings,
) -> None:
    previous = "prev-limiter-secret-32chars-minimum!"
    source = "203.0.113.89"
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    previous_key = admin_auth._hmac_limiter_digest(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        source,
        previous,
    )
    for index in range(5):
        admission = _admit(
            pg_conn,
            keys=(previous_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
        )
        assert admission.admitted

    os.environ["ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"] = previous
    rotated_settings = get_settings()
    rotation_keys = admin_auth.login_limiter_keys(
        submitted_username="operator",
        client_source=source,
        configured_admin_username="operator",
        settings=rotated_settings,
    )
    assert previous_key in rotation_keys

    blocked = _admit(
        pg_conn,
        keys=rotation_keys,
        now=now + timedelta(seconds=20),
        rate_limit=5,
    )
    assert not blocked.admitted
    assert blocked.already_locked
