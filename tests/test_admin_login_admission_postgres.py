"""PostgreSQL-backed admin login admission and limiter atomicity tests."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.main import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://testuser:testpass@localhost:5432/agent_web_test",
)
TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _postgres_available() -> bool:
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=2):
            return True
    except Exception:
        return False


postgres_required = pytest.mark.skipif(
    not _postgres_available(),
    reason="PostgreSQL integration database is unavailable",
)


@pytest.fixture
def postgres_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def postgres_conn(postgres_settings: None) -> Any:
    db.init_db(TEST_DATABASE_URL)
    with db.db_connection(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits")
            cur.execute("DELETE FROM admin_login_flows")
            cur.execute("DELETE FROM audit_events")
        conn.commit()
        yield conn
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_login_rate_limits")
            cur.execute("DELETE FROM admin_login_flows")
            cur.execute("DELETE FROM audit_events")
        conn.commit()


def _source_key(source: str = "203.0.113.10") -> str:
    return admin_auth.build_source_limiter_key(source)


def _account_key() -> str:
    from app.config import get_settings

    return admin_auth.build_account_limiter_key(get_settings())


@postgres_required
@pytest.mark.integration
def test_username_rotation_stops_at_source_threshold(postgres_conn: Any) -> None:
    now = datetime.now(timezone.utc)
    verify_calls = 0
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(username: str, password: str, settings: Any) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return original_verify(username, password, settings)

    with patch.object(admin_auth, "verify_admin_credentials", side_effect=counting_verify):
        for index in range(7):
            admitted, _ = db.admit_admin_login_attempt(
                postgres_conn,
                limiter_keys=[_source_key()],
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            if index < 5:
                assert admitted
            else:
                assert not admitted

    assert verify_calls == 0


@postgres_required
@pytest.mark.integration
def test_concurrent_same_bucket_does_not_overshoot_threshold(postgres_conn: Any) -> None:
    now = datetime.now(timezone.utc)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=5)
        with db.db_connection(TEST_DATABASE_URL) as conn:
            admitted, _ = db.admit_admin_login_attempt(
                conn,
                limiter_keys=[_source_key("198.51.100.20")],
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
        with lock:
            results.append(admitted)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt) for _ in range(8)]
        for future in as_completed(futures):
            future.result()

    assert sum(results) == 5


@postgres_required
@pytest.mark.integration
def test_cross_instance_concurrency_uses_shared_store(postgres_conn: Any) -> None:
    now = datetime.now(timezone.utc)
    admitted_total = 0
    admitted_lock = threading.Lock()

    def worker() -> None:
        local_admitted = 0
        for _ in range(3):
            with db.db_connection(TEST_DATABASE_URL) as conn:
                admitted, _ = db.admit_admin_login_attempt(
                    conn,
                    limiter_keys=[_source_key("203.0.113.44")],
                    now=now,
                    rate_limit=5,
                    window_seconds=900,
                    lockout_seconds=900,
                )
            if admitted:
                local_admitted += 1
        with admitted_lock:
            nonlocal admitted_total
            admitted_total += local_admitted

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert admitted_total == 5


@postgres_required
@pytest.mark.integration
def test_account_bucket_limits_configured_username_across_sources(
    postgres_conn: Any,
) -> None:
    now = datetime.now(timezone.utc)
    account = _account_key()
    for source in ("203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4", "203.0.113.5"):
        admitted, _ = db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=[admin_auth.build_source_limiter_key(source), account],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admitted

    admitted, _ = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=[admin_auth.build_source_limiter_key("203.0.113.6"), account],
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not admitted


@postgres_required
@pytest.mark.integration
def test_already_locked_requests_do_not_increment_rows(postgres_conn: Any) -> None:
    now = datetime.now(timezone.utc)
    key = _source_key("203.0.113.77")
    for _ in range(5):
        db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )

    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (key,),
        )
        before = cur.fetchone()["failure_count"]

    for _ in range(10):
        admitted, transition = db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert not admitted
        assert not transition

    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (key,),
        )
        after = cur.fetchone()["failure_count"]

    assert before == after == 5


@postgres_required
@pytest.mark.integration
def test_success_clears_account_bucket_not_source_bucket(postgres_conn: Any) -> None:
    now = datetime.now(timezone.utc)
    source = _source_key("203.0.113.55")
    account = _account_key()
    db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=[source, account],
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    db.clear_admin_login_rate_limits(postgres_conn, limiter_keys=[account])

    assert not db.is_admin_login_locked(postgres_conn, limiter_keys=[account], now=now)
    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["failure_count"] == 1


@postgres_required
@pytest.mark.integration
def test_window_boundary_resets_failure_count(postgres_conn: Any) -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    key = _source_key("203.0.113.88")
    for _ in range(5):
        db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=[key],
            now=start,
            rate_limit=5,
            window_seconds=60,
            lockout_seconds=60,
        )

    after_window = start + timedelta(seconds=61)
    admitted, _ = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=[key],
        now=after_window,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert admitted

    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (key,),
        )
        row = cur.fetchone()
    assert row["failure_count"] == 1


@postgres_required
@pytest.mark.integration
def test_cleanup_removes_expired_limiter_rows(postgres_conn: Any) -> None:
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = _source_key("203.0.113.99")
    with postgres_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (key, stale, stale),
        )
        postgres_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        postgres_conn,
        now=stale + timedelta(seconds=2000),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@postgres_required
@pytest.mark.integration
def test_login_route_oversized_inputs_rejected_before_verify(
    postgres_conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_calls = 0
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(username: str, password: str, settings: Any) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return original_verify(username, password, settings)

    client = TestClient(app, follow_redirects=False)
    with (
        patch.object(admin_auth, "verify_admin_credentials", side_effect=counting_verify),
        patch("app.admin_routes.db.db_connection") as db_conn,
    ):
        conn = postgres_conn
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        response = client.post(
            "/admin/login",
            data={
                "username": "x" * (admin_auth.LOGIN_USERNAME_MAX_LENGTH + 1),
                "password": "nope",
                "csrf_token": "token",
            },
        )

    assert response.status_code == 400
    assert verify_calls == 0


@postgres_required
@pytest.mark.integration
def test_audit_lockout_transition_recorded_once(postgres_conn: Any) -> None:
    from app.audit_service import ACTION_AUTH_LOGIN_FAILURE

    now = datetime.now(timezone.utc)
    key = _source_key("203.0.113.12")
    for _ in range(4):
        db.admit_admin_login_attempt(
            postgres_conn,
            limiter_keys=[key],
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )

    _, transition = db.admit_admin_login_attempt(
        postgres_conn,
        limiter_keys=[key],
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert transition

    with postgres_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM audit_events WHERE action = %s",
            (ACTION_AUTH_LOGIN_FAILURE,),
        )
        assert cur.fetchone()["count"] == 0
