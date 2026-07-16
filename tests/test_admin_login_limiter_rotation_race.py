"""PostgreSQL race tests for atomic previous-secret limiter guards (#335)."""

from __future__ import annotations

import os
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.rows import dict_row
from starlette.requests import Request

from app import admin_auth, db
from app.admin_security import LIMITER_DOMAIN_SOURCE, digest_limiter_key
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations
from tests.test_admin_auth import TEST_USERNAME

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"

RATE_LIMIT = 5
WINDOW_SECONDS = 900
LOCKOUT_SECONDS = 900
RACE_TIMEOUT_SECONDS = 10
DEADLOCK_TIMEOUT_SECONDS = 15


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres rotation race tests")


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


def _settings(**overrides: Any) -> Settings:
    base = get_settings()
    values = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "analytics_environment": base.analytics_environment,
        "app_environment": base.app_environment,
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SESSION_SECRET,
        "admin_login_limiter_secret": ALT_LIMITER_SECRET,
        "admin_login_limiter_previous_secret": PREVIOUS_LIMITER_SECRET,
        "admin_login_rate_limit": RATE_LIMIT,
        "admin_login_rate_window_seconds": WINDOW_SECONDS,
        "admin_login_lockout_seconds": LOCKOUT_SECONDS,
    }
    values.update(overrides)
    return Settings(**values)


def _rotation_keys(
    settings: Settings,
    *,
    client_source: str = "203.0.113.10",
    submitted_username: str = "ghost",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    guard = admin_auth.login_limiter_rotation_keys(
        settings=settings,
        submitted_username=submitted_username,
        client_source=client_source,
        configured_admin_username=settings.admin_username,
    )
    write = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=submitted_username,
        client_source=client_source,
        configured_admin_username=settings.admin_username,
    )
    return guard, write


def _previous_source_key(settings: Settings, client_source: str = "203.0.113.10") -> str:
    return digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material=client_source,
        secret=PREVIOUS_LIMITER_SECRET,
    )


def _fetch_row(conn: psycopg.Connection, limiter_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT failure_count, window_started_at, locked_until, updated_at
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            """,
            (limiter_key,),
        )
        return cur.fetchone()


def _seed_previous_source_row(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
    failure_count: int,
    locked_until: datetime | None,
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
                now - timedelta(minutes=1),
                locked_until,
                now,
            ),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", ALT_LIMITER_SECRET)
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
        PREVIOUS_LIMITER_SECRET,
    )
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", str(RATE_LIMIT))
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", str(WINDOW_SECONDS))
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", str(LOCKOUT_SECONDS))
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def database_url() -> str:
    return _require_database_url()


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


def _run_old_lockout_wins_race(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = _previous_source_key(settings)
    guard, write = _rotation_keys(settings)
    current_source = write[0]

    _seed_previous_source_row(
        pg_conn,
        limiter_key=previous_source,
        now=now,
        failure_count=RATE_LIMIT - 1,
        locked_until=None,
    )

    old_locked = threading.Event()
    new_waiting = threading.Event()
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def old_writer() -> None:
        try:
            with _connect(database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT failure_count, locked_until
                        FROM admin_login_rate_limits
                        WHERE limiter_key = %s
                        FOR UPDATE
                        """,
                        (previous_source,),
                    )
                    row = cur.fetchone()
                    assert row is not None
                    old_locked.set()
                    assert new_waiting.wait(timeout=RACE_TIMEOUT_SECONDS)
                    locked_until = now + timedelta(seconds=LOCKOUT_SECONDS)
                    cur.execute(
                        """
                        UPDATE admin_login_rate_limits
                        SET failure_count = %s,
                            locked_until = %s,
                            updated_at = %s
                        WHERE limiter_key = %s
                        """,
                        (RATE_LIMIT, locked_until, now, previous_source),
                    )
                conn.commit()
                results["old"] = "lockout_committed"
        except BaseException as exc:  # pragma: no cover - surfaced via pytest.fail
            errors.append(exc)

    def new_claimant() -> None:
        try:
            assert old_locked.wait(timeout=RACE_TIMEOUT_SECONDS)
            with _connect(database_url) as conn:
                new_waiting.set()
                results["new"] = db.try_admit_admin_login(
                    conn,
                    limiter_keys=write,
                    guard_keys=guard,
                    now=now,
                    rate_limit=RATE_LIMIT,
                    window_seconds=WINDOW_SECONDS,
                    lockout_seconds=LOCKOUT_SECONDS,
                )
        except BaseException as exc:  # pragma: no cover - surfaced via pytest.fail
            errors.append(exc)

    old_thread = threading.Thread(target=old_writer, name="old-writer")
    new_thread = threading.Thread(target=new_claimant, name="new-claimant")
    old_thread.start()
    new_thread.start()
    old_thread.join(timeout=RACE_TIMEOUT_SECONDS)
    new_thread.join(timeout=RACE_TIMEOUT_SECONDS)

    if errors:
        pytest.fail(f"race workers raised: {errors!r}")
    assert old_thread.is_alive() is False
    assert new_thread.is_alive() is False

    admission = results["new"]
    assert isinstance(admission, db.AdminLoginAdmission)
    assert not admission.admitted
    assert admission.already_locked

    previous_after = _fetch_row(pg_conn, previous_source)
    current_after = _fetch_row(pg_conn, current_source)
    assert previous_after is not None
    assert int(previous_after["failure_count"]) == RATE_LIMIT
    assert previous_after["locked_until"] is not None
    assert current_after is None or int(current_after["failure_count"]) == 0


@pytest.mark.integration
def test_old_instance_lockout_wins_race_against_new_guard_check(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    _run_old_lockout_wins_race(database_url, pg_conn)


@pytest.mark.integration
def test_new_instance_admits_when_guard_lockout_not_yet_committed(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    previous_source = _previous_source_key(settings)
    guard, write = _rotation_keys(settings)
    current_source = write[0]

    _seed_previous_source_row(
        pg_conn,
        limiter_key=previous_source,
        now=now,
        failure_count=RATE_LIMIT - 1,
        locked_until=None,
    )

    new_done = threading.Event()
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def new_claimant() -> None:
        try:
            with _connect(database_url) as conn:
                results["new"] = db.try_admit_admin_login(
                    conn,
                    limiter_keys=write,
                    guard_keys=guard,
                    now=now,
                    rate_limit=RATE_LIMIT,
                    window_seconds=WINDOW_SECONDS,
                    lockout_seconds=LOCKOUT_SECONDS,
                )
            new_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced via pytest.fail
            errors.append(exc)

    def old_writer() -> None:
        try:
            assert new_done.wait(timeout=RACE_TIMEOUT_SECONDS)
            with _connect(database_url) as conn:
                results["old"] = db.try_admit_admin_login(
                    conn,
                    limiter_keys=guard,
                    now=now,
                    rate_limit=RATE_LIMIT,
                    window_seconds=WINDOW_SECONDS,
                    lockout_seconds=LOCKOUT_SECONDS,
                )
        except BaseException as exc:  # pragma: no cover - surfaced via pytest.fail
            errors.append(exc)

    new_thread = threading.Thread(target=new_claimant, name="new-claimant")
    old_thread = threading.Thread(target=old_writer, name="old-writer")
    new_thread.start()
    old_thread.start()
    new_thread.join(timeout=RACE_TIMEOUT_SECONDS)
    old_thread.join(timeout=RACE_TIMEOUT_SECONDS)

    if errors:
        pytest.fail(f"race workers raised: {errors!r}")

    new_admission = results["new"]
    old_admission = results["old"]
    assert new_admission.admitted
    assert old_admission.lockout_transition

    previous_after = _fetch_row(pg_conn, previous_source)
    current_after = _fetch_row(pg_conn, current_source)
    assert previous_after is not None
    assert int(previous_after["failure_count"]) == RATE_LIMIT
    assert current_after is not None
    assert int(current_after["failure_count"]) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("locked_until", "expect_admitted"),
    [
        (datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 3, 11, 59, tzinfo=timezone.utc), True),
        (datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc), True),
    ],
    ids=["active_lockout", "expired_lockout", "exact_expiry_boundary"],
)
def test_guard_lockout_states(
    pg_conn: psycopg.Connection,
    locked_until: datetime,
    expect_admitted: bool,
) -> None:
    settings = _settings()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    previous_source = _previous_source_key(settings)
    guard, write = _rotation_keys(settings)
    current_source = write[0]

    _seed_previous_source_row(
        pg_conn,
        limiter_key=previous_source,
        now=now,
        failure_count=RATE_LIMIT,
        locked_until=locked_until,
    )

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write,
        guard_keys=guard,
        now=now,
        rate_limit=RATE_LIMIT,
        window_seconds=WINDOW_SECONDS,
        lockout_seconds=LOCKOUT_SECONDS,
    )
    assert admission.admitted is expect_admitted
    current_after = _fetch_row(pg_conn, current_source)
    if expect_admitted:
        assert current_after is not None
        assert int(current_after["failure_count"]) == 1
    else:
        assert current_after is None or int(current_after["failure_count"]) == 0


@pytest.mark.integration
def test_missing_guard_row_allows_current_admission(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    guard, write = _rotation_keys(settings)
    current_source = write[0]

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write,
        guard_keys=guard,
        now=now,
        rate_limit=RATE_LIMIT,
        window_seconds=WINDOW_SECONDS,
        lockout_seconds=LOCKOUT_SECONDS,
    )
    assert admission.admitted
    current_after = _fetch_row(pg_conn, current_source)
    assert current_after is not None
    assert int(current_after["failure_count"]) == 1


@pytest.mark.integration
def test_guard_denial_leaves_previous_row_unchanged(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    previous_source = _previous_source_key(settings)
    guard, write = _rotation_keys(settings)
    locked_until = now + timedelta(minutes=15)
    window_started = now - timedelta(minutes=2)
    updated_at = now - timedelta(seconds=30)

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (previous_source, RATE_LIMIT, window_started, locked_until, updated_at),
        )
    pg_conn.commit()

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write,
        guard_keys=guard,
        now=now,
        rate_limit=RATE_LIMIT,
        window_seconds=WINDOW_SECONDS,
        lockout_seconds=LOCKOUT_SECONDS,
    )
    assert not admission.admitted

    previous_after = _fetch_row(pg_conn, previous_source)
    assert previous_after is not None
    assert int(previous_after["failure_count"]) == RATE_LIMIT
    assert previous_after["window_started_at"] == window_started
    assert previous_after["locked_until"] == locked_until
    assert previous_after["updated_at"] == updated_at


@pytest.mark.integration
def test_union_lock_ordering_avoids_deadlock_under_shuffled_inputs(
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    guard, write = _rotation_keys(settings)
    all_keys = list(guard) + list(write)

    errors: list[BaseException] = []
    done = threading.Event()

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        try:
            shuffled_guard = tuple(rng.sample(list(guard), len(guard)))
            shuffled_write = tuple(rng.sample(list(write), len(write)))
            with _connect(database_url) as conn:
                db.try_admit_admin_login(
                    conn,
                    limiter_keys=shuffled_write,
                    guard_keys=shuffled_guard,
                    now=now + timedelta(seconds=seed),
                    rate_limit=RATE_LIMIT,
                    window_seconds=WINDOW_SECONDS,
                    lockout_seconds=LOCKOUT_SECONDS,
                )
        except BaseException as exc:  # pragma: no cover - surfaced via pytest.fail
            errors.append(exc)
        finally:
            done.set()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    deadline = time.monotonic() + DEADLOCK_TIMEOUT_SECONDS
    for thread in threads:
        thread.start()
    for thread in threads:
        remaining = deadline - time.monotonic()
        thread.join(timeout=max(remaining, 0.1))

    if errors:
        pytest.fail(f"deadlock workers raised: {errors!r}")
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"threads still blocked after timeout: {alive}"


@pytest.mark.integration
@pytest.mark.parametrize("attempt", range(3))
def test_old_instance_lockout_race_is_stable_under_repeated_runs(
    attempt: int,
    database_url: str,
    pg_conn: psycopg.Connection,
) -> None:
    _run_old_lockout_wins_race(database_url, pg_conn)


@pytest.mark.unit
def test_try_admit_admin_login_locks_guard_union_in_sorted_order() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cur.fetchall.side_effect = [
        [
            {
                "limiter_key": "guard-key",
                "failure_count": 5,
                "window_started_at": now,
                "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
            }
        ],
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("write-key",),
        guard_keys=("guard-key",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    executed = [call.args[0] for call in cur.execute.call_args_list]
    lock_sql = next(sql for sql in executed if "FOR UPDATE" in sql)
    assert "ORDER BY limiter_key" in lock_sql
    lock_args = next(call.args[1] for call in cur.execute.call_args_list if "FOR UPDATE" in call.args[0])
    assert lock_args == (["guard-key", "write-key"],)
    assert not admission.admitted
    assert admission.already_locked
    assert "UPDATE admin_login_rate_limits" not in " ".join(executed)


@pytest.mark.integration
def test_try_admit_login_attempt_passes_guard_keys_to_repository(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
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
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> db.AdminLoginAdmission:
        captured.update(kwargs)
        return db.try_admit_admin_login(conn, **kwargs)

    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = pg_conn
        db_conn.return_value.__exit__.return_value = None
        with patch("app.admin_auth.db.try_admit_admin_login", side_effect=_capture):
            with patch(
                "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
                return_value=0,
            ):
                result = admin_auth.try_admit_login_attempt(
                    request,
                    settings,
                    username="ghost",
                )

    assert result.admitted
    guard = admin_auth.login_limiter_rotation_keys(
        settings=settings,
        submitted_username="ghost",
        client_source="203.0.113.10",
        configured_admin_username=settings.admin_username,
    )
    assert captured["guard_keys"] == guard
    assert "is_admin_login_throttled" not in str(captured)


@pytest.mark.integration
def test_guard_denied_login_admission_blocks_before_password_work(
    database_url: str,
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    settings = get_settings()
    guard, write = _rotation_keys(
        settings,
        client_source="testclient",
        submitted_username="ghost",
    )
    locked_until = now + timedelta(minutes=15)
    for guard_key in guard:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_login_rate_limits (
                    limiter_key, failure_count, window_started_at, locked_until, updated_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    guard_key,
                    RATE_LIMIT,
                    now - timedelta(minutes=1),
                    locked_until,
                    now,
                ),
            )
    pg_conn.commit()

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
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    admission = admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    assert not admission.admitted
    assert admission.already_locked
    assert not admission.store_unavailable

    for current_key in write:
        current_after = _fetch_row(pg_conn, current_key)
        assert current_after is None or int(current_after["failure_count"]) == 0

    for guard_key in guard:
        guard_after = _fetch_row(pg_conn, guard_key)
        assert guard_after is not None
        assert int(guard_after["failure_count"]) == RATE_LIMIT
        assert guard_after["updated_at"] == now
