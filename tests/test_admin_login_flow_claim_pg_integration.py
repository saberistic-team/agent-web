"""PostgreSQL integration tests for atomic admin login-flow claims (#243)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.main import app
from app.migrations.runner import apply_migrations
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    _extract_csrf_token,
    _extract_session_cookie,
    _parse_login_form,
)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

pytestmark = [pytest.mark.integration]

client_a = TestClient(app, follow_redirects=False)
client_b = TestClient(app, follow_redirects=False)

_CLAIM_SQL = """
UPDATE admin_login_flows
SET consumed_at = %s
WHERE flow_token_hash = %s
  AND csrf_token_hash = %s
  AND consumed_at IS NULL
  AND expires_at > %s
RETURNING id, flow_token_hash, csrf_token_hash, created_at, expires_at, consumed_at
"""

_ADMITTED = db.AdminLoginAdmission(
    admitted=True,
    throttled=False,
    already_locked=False,
    lockout_transition=False,
)


@contextmanager
def _admit_all_login_attempts() -> Iterator[None]:
    """Bypass shared limiter storage without replacing ``db.db_connection``."""
    with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
        yield


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login-flow claim tests")


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


@pytest.fixture
def pg_env(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _seed_login_flow(
    conn: psycopg.Connection,
    *,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> tuple[int, str, str, str, str, datetime]:
    raw_flow = admin_auth.generate_session_token()
    raw_csrf = admin_auth.generate_csrf_value()
    flow_hash = admin_auth.hash_session_token(raw_flow)
    csrf_hash = admin_auth.hash_csrf_token(raw_csrf)
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=15))
    if consumed_at is None:
        flow_id = db.create_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            expires_at=expiry,
        )
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_login_flows (
                    flow_token_hash, csrf_token_hash, expires_at, consumed_at
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (flow_hash, csrf_hash, expiry, consumed_at),
            )
            row = cur.fetchone()
            conn.commit()
        assert row is not None
        flow_id = int(row["id"])
    return flow_id, raw_flow, raw_csrf, flow_hash, csrf_hash, expiry


def _get_flow_row(conn: psycopg.Connection, flow_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, consumed_at, expires_at, created_at
            FROM admin_login_flows
            WHERE id = %s
            """,
            (flow_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _count_flow_rows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM admin_login_flows")
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _count_active_sessions(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM admin_sessions WHERE revoked_at IS NULL"
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _count_login_success_audits(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action = %s",
            (audit_service.ACTION_AUTH_LOGIN_SUCCESS,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _race_claims(
    database_url: str,
    *,
    flow_hash: str,
    csrf_hash: str,
    now: datetime,
    workers: int,
) -> list[dict[str, Any] | None]:
    barrier = threading.Barrier(workers, timeout=10)
    results: list[dict[str, Any] | None] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            with _connect(database_url) as conn:
                row = db.claim_admin_login_flow(
                    conn,
                    flow_token_hash=flow_hash,
                    csrf_token_hash=csrf_hash,
                    now=now,
                )
            with results_lock:
                results.append(row)
        except BaseException as exc:  # pragma: no cover - threaded harness guard
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"worker_errors={len(errors)}"
    assert len(results) == workers, f"results={len(results)} expected={workers}"
    return results


def _fetch_pg_login_form(test_client: TestClient = client_a) -> tuple[str, dict[str, str]]:
    response = test_client.get("/admin/login")
    assert response.status_code == 200
    return _parse_login_form(response)


def _concurrent_login_posts(
    *,
    csrf_token: str,
    cookies: dict[str, str],
    password: str = TEST_PASSWORD,
    verify_side_effect: Any,
    workers: int = 2,
) -> list[Any]:
    barrier = threading.Barrier(workers, timeout=10)
    results: list[Any] = []
    results_lock = threading.Lock()
    clients = [client_a, client_b]

    def _worker(index: int) -> None:
        barrier.wait(timeout=10)
        test_client = clients[index % len(clients)]
        response = test_client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": password,
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
        with results_lock:
            results.append(response)

    with _admit_all_login_attempts():
        with patch(
            "app.admin_routes.admin_auth.verify_admin_credentials",
            side_effect=verify_side_effect,
        ):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for index in range(workers):
                    pool.submit(_worker, index)
    return results


def test_repository_race_two_connections_one_winner(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )

    results = _race_claims(
        database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=2,
    )

    winners = [row for row in results if row is not None]
    losers = [row for row in results if row is None]
    assert len(winners) == 1, (
        f"flow_id={flow_id} winners={len(winners)} losers={len(losers)}"
    )
    assert len(losers) == 1, f"flow_id={flow_id} winners={len(winners)} losers={len(losers)}"

    row = _get_flow_row(pg_conn, flow_id)
    assert row["consumed_at"] is not None, f"flow_id={flow_id} consumed_at={row['consumed_at']}"
    assert _count_flow_rows(pg_conn) == 1


def test_repository_burst_three_or_more_claimants_one_winner(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    workers = 5

    results = _race_claims(
        database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=workers,
    )

    winners = [row for row in results if row is not None]
    losers = [row for row in results if row is None]
    assert len(winners) == 1, (
        f"flow_id={flow_id} workers={workers} winners={len(winners)} losers={len(losers)}"
    )
    assert len(losers) == workers - 1


def test_repository_second_transaction_waits_then_observes_zero_rows(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    winner_started = threading.Event()
    loser_may_finish = threading.Event()
    loser_result: dict[str, Any | None] = {}

    def winner() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    _CLAIM_SQL,
                    (now, flow_hash, csrf_hash, now),
                )
                row = cur.fetchone()
                assert row is not None, f"flow_id={flow_id}"
            winner_started.set()
            loser_may_finish.wait(timeout=10)
            conn.commit()

    def loser() -> None:
        winner_started.wait(timeout=10)
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
            loser_result["row"] = row
            loser_may_finish.set()

    winner_thread = threading.Thread(target=winner)
    loser_thread = threading.Thread(target=loser)
    winner_thread.start()
    loser_thread.start()
    winner_thread.join(timeout=15)
    loser_thread.join(timeout=15)

    assert loser_result.get("row") is None, f"flow_id={flow_id} loser_row={loser_result.get('row')}"


def test_repository_winner_rollback_leaves_flow_claimable(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    barrier = threading.Barrier(2, timeout=10)
    rolled_back: dict[str, bool] = {"value": False}

    def rollback_winner() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    _CLAIM_SQL,
                    (now, flow_hash, csrf_hash, now),
                )
                row = cur.fetchone()
                assert row is not None, f"flow_id={flow_id}"
            barrier.wait(timeout=10)
            conn.rollback()
            rolled_back["value"] = True

    def subsequent_claimer() -> None:
        barrier.wait(timeout=10)
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
            assert row is not None, f"flow_id={flow_id} rolled_back={rolled_back['value']}"

    threads = [
        threading.Thread(target=rollback_winner),
        threading.Thread(target=subsequent_claimer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    row = _get_flow_row(pg_conn, flow_id)
    assert row["consumed_at"] is not None, f"flow_id={flow_id}"


def test_repository_exact_expiry_boundary(
    pg_conn: psycopg.Connection,
) -> None:
    boundary = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=boundary,
    )

    at_boundary = db.claim_admin_login_flow(
        pg_conn,
        flow_token_hash=flow_hash,
        csrf_token_hash=csrf_hash,
        now=boundary,
    )
    assert at_boundary is None, f"flow_id={flow_id}"

    before_boundary = db.claim_admin_login_flow(
        pg_conn,
        flow_token_hash=flow_hash,
        csrf_token_hash=csrf_hash,
        now=boundary - timedelta(microseconds=1),
    )
    assert before_boundary is not None, f"flow_id={flow_id}"


def test_repository_wrong_csrf_and_identity_under_concurrent_pressure(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    wrong_flow_hash = admin_auth.hash_session_token(admin_auth.generate_session_token())
    wrong_csrf_hash = admin_auth.hash_csrf_token(admin_auth.generate_csrf_value())
    workers = 4
    barrier = threading.Barrier(workers, timeout=10)
    results: list[dict[str, Any] | None] = []
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait(timeout=10)
        use_flow = flow_hash if index % 2 == 0 else wrong_flow_hash
        use_csrf = csrf_hash if index == 0 else wrong_csrf_hash
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=use_flow,
                csrf_token_hash=use_csrf,
                now=now,
            )
        with results_lock:
            results.append(row)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    winners = [row for row in results if row is not None]
    assert len(winners) == 1, f"flow_id={flow_id} winners={len(winners)}"
    row = _get_flow_row(pg_conn, flow_id)
    assert row["consumed_at"] is not None, f"flow_id={flow_id}"


def test_repository_cleanup_race_does_not_delete_active_flow(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    claim_started = threading.Event()
    claim_may_commit = threading.Event()
    cleanup_done = threading.Event()

    def slow_claim() -> None:
        with _connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute(
                    _CLAIM_SQL,
                    (now, flow_hash, csrf_hash, now),
                )
                row = cur.fetchone()
                assert row is not None, f"flow_id={flow_id}"
            claim_started.set()
            claim_may_commit.wait(timeout=10)
            conn.commit()

    def cleanup_worker() -> None:
        claim_started.wait(timeout=10)
        with _connect(database_url) as conn:
            deleted = db.cleanup_stale_admin_login_flows(
                conn,
                now=now,
                expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
                consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
                batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
            )
        cleanup_done.set()
        assert deleted == 0, f"flow_id={flow_id} deleted={deleted}"

    claim_thread = threading.Thread(target=slow_claim)
    cleanup_thread = threading.Thread(target=cleanup_worker)
    claim_thread.start()
    cleanup_thread.start()
    cleanup_thread.join(timeout=15)
    assert cleanup_done.is_set(), f"flow_id={flow_id}"
    assert _get_flow_row(pg_conn, flow_id) is not None, f"flow_id={flow_id}"
    claim_may_commit.set()
    claim_thread.join(timeout=15)

    row = _get_flow_row(pg_conn, flow_id)
    assert row["consumed_at"] is not None, f"flow_id={flow_id}"


def test_repository_connection_failure_leaves_row_unconsumed(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expiry = _seed_login_flow(
        pg_conn,
        expires_at=now + timedelta(minutes=5),
    )
    broken = psycopg.connect(database_url, row_factory=dict_row)
    broken.close()

    with pytest.raises(psycopg.OperationalError):
        db.claim_admin_login_flow(
            broken,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=now,
        )

    row = _get_flow_row(pg_conn, flow_id)
    assert row["consumed_at"] is None, f"flow_id={flow_id}"


def test_route_race_valid_credentials_one_verify_session_and_audit(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    verify_calls = {"count": 0}

    def counting_verify(_user: str, _password: str, _settings: Any) -> bool:
        verify_calls["count"] += 1
        return True

    csrf_token, cookies = _fetch_pg_login_form()
    before_sessions = _count_active_sessions(pg_conn)
    before_audits = _count_login_success_audits(pg_conn)

    responses = _concurrent_login_posts(
        csrf_token=csrf_token,
        cookies=cookies,
        verify_side_effect=counting_verify,
    )

    status_codes = sorted(response.status_code for response in responses)
    assert status_codes == [303, 400], f"status_codes={status_codes}"
    assert verify_calls["count"] == 1, f"verify_calls={verify_calls['count']}"
    assert _count_active_sessions(pg_conn) == before_sessions + 1
    assert _count_login_success_audits(pg_conn) == before_audits + 1

    successes = [response for response in responses if response.status_code == 303]
    assert _extract_session_cookie(successes[0]) is not None


def test_route_race_invalid_credentials_one_verify_replacement_isolated(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    verify_calls = {"count": 0}

    def counting_verify(_user: str, _password: str, _settings: Any) -> bool:
        verify_calls["count"] += 1
        return False

    csrf_token, cookies = _fetch_pg_login_form()
    before_sessions = _count_active_sessions(pg_conn)

    responses = _concurrent_login_posts(
        csrf_token=csrf_token,
        cookies=cookies,
        password="wrong-password",
        verify_side_effect=counting_verify,
    )

    status_codes = sorted(response.status_code for response in responses)
    assert status_codes == [400, 401], f"status_codes={status_codes}"
    assert verify_calls["count"] == 1, f"verify_calls={verify_calls['count']}"
    assert _count_active_sessions(pg_conn) == before_sessions

    winner = next(response for response in responses if response.status_code == 401)
    loser = next(response for response in responses if response.status_code == 400)
    replacement_csrf = _extract_csrf_token(winner.text)
    replacement_cookie = winner.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert replacement_cookie

    with _admit_all_login_attempts():
        loser_replay = client_a.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
        replacement_replay = client_b.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": replacement_csrf,
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: replacement_cookie},
        )

    assert loser_replay.status_code == 400
    assert replacement_replay.status_code == 401
    assert loser.status_code == 400
    assert _count_login_success_audits(pg_conn) == 0


def test_route_claim_database_failure_skips_verify_and_session_mutation(
    pg_conn: psycopg.Connection,
    pg_env: None,
) -> None:
    verify_calls = {"count": 0}

    def counting_verify(_user: str, _password: str, _settings: Any) -> bool:
        verify_calls["count"] += 1
        return True

    csrf_token, cookies = _fetch_pg_login_form()

    with _admit_all_login_attempts():
        with patch(
            "app.admin_routes.db.claim_admin_login_flow",
            side_effect=RuntimeError("database unavailable"),
        ):
            response = client_a.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )

    assert verify_calls["count"] == 0
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    assert SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.unit
def test_require_test_database_guard_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_admin_login_flow_claim_pg_integration as mod
    from _pytest.outcomes import Failed

    monkeypatch.setattr(mod, "_REQUIRED", True)
    monkeypatch.setattr(mod, "_DATABASE_URL", "")
    with pytest.raises(Failed):
        mod._require_database_url()


@pytest.mark.unit
def test_login_flow_claim_pg_module_requires_integration_marker() -> None:
    assert pytest.mark.integration in pytestmark


@pytest.mark.unit
def test_require_test_database_returns_url_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.test_admin_login_flow_claim_pg_integration as mod

    monkeypatch.setattr(mod, "_REQUIRED", True)
    monkeypatch.setattr(mod, "_DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    assert mod._require_database_url() == "postgresql://test:test@127.0.0.1:5432/agent_web_test"
