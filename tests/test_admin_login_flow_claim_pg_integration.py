"""PostgreSQL integration tests for atomic admin login-flow claim concurrency (#243)."""

from __future__ import annotations

import inspect
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg
import pytest
from _pytest.outcomes import Failed
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

TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

client_a = TestClient(app, follow_redirects=False)
client_b = TestClient(app, follow_redirects=False)


def _require_database_url() -> str:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
    if database_url:
        return database_url
    if required:
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


@pytest.fixture(autouse=True)
def admin_env(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "100")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@dataclass(frozen=True)
class _ClaimOutcome:
    worker_id: int
    connection_id: int
    claimed: bool
    flow_id: int | None


def _seed_flow_row(
    conn: psycopg.Connection,
    *,
    now: datetime,
    ttl_seconds: int = 900,
) -> tuple[str, str, str, str, int]:
    raw_flow = admin_auth.generate_session_token()
    raw_csrf = admin_auth.generate_csrf_value()
    flow_hash = admin_auth.hash_session_token(raw_flow)
    csrf_hash = admin_auth.hash_csrf_token(raw_csrf)
    expires_at = now + timedelta(seconds=ttl_seconds)
    flow_id = db.create_admin_login_flow(
        conn,
        flow_token_hash=flow_hash,
        csrf_token_hash=csrf_hash,
        expires_at=expires_at,
    )
    return raw_flow, raw_csrf, flow_hash, csrf_hash, flow_id


def _flow_row_state(conn: psycopg.Connection, flow_hash: str) -> dict[str, Any] | None:
    return db.get_admin_login_flow_by_token_hash(conn, flow_hash)


def _count_login_success_audits(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action = %s",
            (audit_service.ACTION_AUTH_LOGIN_SUCCESS,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _count_sessions(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM admin_sessions")
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _race_claims(
    *,
    database_url: str,
    flow_hash: str,
    csrf_hash: str,
    now: datetime,
    workers: int,
) -> list[_ClaimOutcome]:
    barrier = threading.Barrier(workers)
    outcomes: list[_ClaimOutcome] = []
    outcomes_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        barrier.wait()
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
            outcome = _ClaimOutcome(
                worker_id=worker_id,
                connection_id=id(conn),
                claimed=row is not None,
                flow_id=int(row["id"]) if row is not None else None,
            )
            with outcomes_lock:
                outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    return sorted(outcomes, key=lambda item: item.worker_id)


def _login_post(
    test_client: TestClient,
    *,
    csrf_token: str,
    cookies: dict[str, str],
    password: str = TEST_PASSWORD,
) -> Any:
    return test_client.post(
        "/admin/login",
        data={
            "username": TEST_USERNAME,
            "password": password,
            "csrf_token": csrf_token,
        },
        cookies=cookies,
    )


def _fetch_pg_login_form(test_client: TestClient = client_a) -> tuple[str, dict[str, str]]:
    response = test_client.get("/admin/login")
    assert response.status_code == 200
    return _parse_login_form(response)


def _format_outcomes(outcomes: list[_ClaimOutcome]) -> str:
    winners = [item for item in outcomes if item.claimed]
    losers = [item for item in outcomes if not item.claimed]
    return (
        f"winners={len(winners)} losers={len(losers)} "
        f"worker_results={[(o.worker_id, o.claimed, o.flow_id) for o in outcomes]}"
    )


@pytest.mark.integration
def test_pg_repository_race_two_connections_one_returning_row(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)

    outcomes = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=2,
    )

    winners = [item for item in outcomes if item.claimed]
    losers = [item for item in outcomes if not item.claimed]
    assert len(winners) == 1, _format_outcomes(outcomes)
    assert len(losers) == 1, _format_outcomes(outcomes)
    assert len({item.connection_id for item in outcomes}) == 2, _format_outcomes(outcomes)

    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["id"] == flow_id
    assert final_row["consumed_at"] is not None
    assert winners[0].flow_id == flow_id


@pytest.mark.integration
def test_pg_repository_race_burst_three_or_more_claimants(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 5, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)

    outcomes = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=5,
    )

    winners = [item for item in outcomes if item.claimed]
    losers = [item for item in outcomes if not item.claimed]
    assert len(winners) == 1, _format_outcomes(outcomes)
    assert len(losers) == 4, _format_outcomes(outcomes)

    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["consumed_at"] is not None
    assert winners[0].flow_id == flow_id


@pytest.mark.integration
def test_pg_winner_commit_loser_waits_then_observes_zero_rows(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 10, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)
    winner_committed = threading.Event()
    loser_outcome: dict[str, Any] = {}

    def winner() -> None:
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
            assert row is not None
            winner_committed.set()

    def loser() -> None:
        winner_committed.wait(timeout=5)
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now + timedelta(milliseconds=1),
            )
            loser_outcome["claimed"] = row is not None

    winner_thread = threading.Thread(target=winner)
    loser_thread = threading.Thread(target=loser)
    winner_thread.start()
    loser_thread.start()
    winner_thread.join(timeout=10)
    loser_thread.join(timeout=10)

    assert loser_outcome.get("claimed") is False
    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["id"] == flow_id
    assert final_row["consumed_at"] is not None


@pytest.mark.integration
def test_pg_winner_rollback_leaves_row_claimable(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    """Rolled-back UPDATE releases the row; a later claimant can consume it."""
    now = datetime(2026, 7, 15, 12, 15, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)
    hold_lock = threading.Event()
    release_rollback = threading.Event()
    second_outcome: dict[str, Any] = {}

    def first_transaction() -> None:
        with _connect(database_url) as conn:
            conn.execute("BEGIN")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_login_flows
                    SET consumed_at = %s
                    WHERE flow_token_hash = %s
                      AND csrf_token_hash = %s
                      AND consumed_at IS NULL
                      AND expires_at > %s
                    RETURNING id
                    """,
                    (now, flow_hash, csrf_hash, now),
                )
                row = cur.fetchone()
                assert row is not None
            hold_lock.set()
            release_rollback.wait(timeout=5)
            conn.rollback()

    def second_transaction() -> None:
        hold_lock.wait(timeout=5)
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now + timedelta(milliseconds=1),
            )
            second_outcome["claimed"] = row is not None
            second_outcome["flow_id"] = int(row["id"]) if row is not None else None

    first_thread = threading.Thread(target=first_transaction)
    second_thread = threading.Thread(target=second_transaction)
    first_thread.start()
    second_thread.start()
    release_rollback.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert second_outcome.get("claimed") is True
    assert second_outcome.get("flow_id") == flow_id
    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["consumed_at"] is not None


@pytest.mark.integration
def test_pg_exact_expiry_boundary_rejects_at_expires_at(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    boundary = datetime(2026, 7, 15, 12, 20, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(
        pg_conn,
        now=boundary - timedelta(minutes=5),
        ttl_seconds=300,
    )
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_login_flows SET expires_at = %s WHERE id = %s",
                (boundary, flow_id),
            )
        conn.commit()
        at_boundary = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=boundary,
        )
    assert at_boundary is None
    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["consumed_at"] is None


@pytest.mark.integration
def test_pg_exact_expiry_boundary_allows_before_expires_at(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    boundary = datetime(2026, 7, 15, 12, 21, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(
        pg_conn,
        now=boundary - timedelta(minutes=5),
        ttl_seconds=300,
    )
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_login_flows SET expires_at = %s WHERE id = %s",
                (boundary, flow_id),
            )
        conn.commit()
        before_boundary = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=boundary - timedelta(seconds=1),
        )
    assert before_boundary is not None
    assert int(before_boundary["id"]) == flow_id


@pytest.mark.integration
def test_pg_wrong_csrf_concurrent_pressure_claims_nothing(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 25, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, _csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)
    wrong_csrf_hash = admin_auth.hash_csrf_token("wrong-csrf-under-pressure")
    outcomes = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=wrong_csrf_hash,
        now=now,
        workers=3,
    )

    assert all(not item.claimed for item in outcomes), _format_outcomes(outcomes)
    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["id"] == flow_id
    assert final_row["consumed_at"] is None


@pytest.mark.integration
def test_pg_claim_commit_failure_leaves_row_unconsumed(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)

    with _connect(database_url) as conn:
        with patch.object(conn, "commit", side_effect=RuntimeError("simulated commit failure")):
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                db.claim_admin_login_flow(
                    conn,
                    flow_token_hash=flow_hash,
                    csrf_token_hash=csrf_hash,
                    now=now,
                )

    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["id"] == flow_id
    assert final_row["consumed_at"] is None


@pytest.mark.integration
def test_pg_cleanup_race_cannot_delete_active_or_double_consume(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 35, tzinfo=timezone.utc)
    _raw_flow, _raw_csrf, flow_hash, csrf_hash, flow_id = _seed_flow_row(pg_conn, now=now)
    claim_started = threading.Event()
    claim_finished = threading.Event()
    cleanup_deleted = {"count": 0}

    def claim_worker() -> None:
        claim_started.set()
        with _connect(database_url) as conn:
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
            assert row is not None
        claim_finished.set()

    def cleanup_worker() -> None:
        claim_started.wait(timeout=5)
        with _connect(database_url) as conn:
            cleanup_deleted["count"] = db.cleanup_stale_admin_login_flows(
                conn,
                now=now,
                expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
                consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
                batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
            )

    claim_thread = threading.Thread(target=claim_worker)
    cleanup_thread = threading.Thread(target=cleanup_worker)
    claim_thread.start()
    cleanup_thread.start()
    cleanup_thread.join(timeout=10)
    claim_thread.join(timeout=10)
    claim_finished.wait(timeout=1)

    assert cleanup_deleted["count"] == 0
    final_row = _flow_row_state(pg_conn, flow_hash)
    assert final_row is not None
    assert final_row["consumed_at"] is not None

    with _connect(database_url) as conn:
        replay = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=now + timedelta(seconds=1),
        )
    assert replay is None


@pytest.mark.integration
def test_pg_route_race_valid_credentials_one_session_and_audit(
    pg_conn: psycopg.Connection,
) -> None:
    csrf_token, cookies = _fetch_pg_login_form()
    verify_calls: list[tuple[str, str]] = []
    verify_lock = threading.Lock()
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def counting_verify(user: str, pwd: str, settings: Any) -> bool:
        with verify_lock:
            verify_calls.append((user, pwd))
        return True

    def worker(test_client: TestClient) -> None:
        barrier.wait()
        results.append(_login_post(test_client, csrf_token=csrf_token, cookies=cookies))

    with patch("app.admin_auth.verify_admin_credentials", side_effect=counting_verify):
        threads = [
            threading.Thread(target=worker, args=(client_a,)),
            threading.Thread(target=worker, args=(client_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert len(verify_calls) == 1, f"verify_calls={len(verify_calls)}"
    successes = [response for response in results if response.status_code == 303]
    failures = [response for response in results if response.status_code != 303]
    assert len(successes) == 1, f"statuses={[r.status_code for r in results]}"
    assert len(failures) == 1, f"statuses={[r.status_code for r in results]}"
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in failures[0].text
    assert _extract_session_cookie(successes[0]) is not None
    assert _count_sessions(pg_conn) == 1
    assert _count_login_success_audits(pg_conn) == 1


@pytest.mark.integration
def test_pg_route_race_invalid_credentials_one_verify_and_replacement(
    pg_conn: psycopg.Connection,
) -> None:
    csrf_token, cookies = _fetch_pg_login_form()
    verify_calls = {"count": 0}
    verify_lock = threading.Lock()
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def counting_verify(user: str, pwd: str, settings: Any) -> bool:
        with verify_lock:
            verify_calls["count"] += 1
        return False

    def worker(test_client: TestClient) -> None:
        barrier.wait()
        results.append(
            _login_post(
                test_client,
                csrf_token=csrf_token,
                cookies=cookies,
                password="wrong-password",
            )
        )

    with patch("app.admin_auth.verify_admin_credentials", side_effect=counting_verify):
        threads = [
            threading.Thread(target=worker, args=(client_a,)),
            threading.Thread(target=worker, args=(client_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert verify_calls["count"] == 1
    credential_failures = [response for response in results if response.status_code == 401]
    claim_failures = [response for response in results if response.status_code == 400]
    assert len(credential_failures) == 1, f"statuses={[r.status_code for r in results]}"
    assert len(claim_failures) == 1, f"statuses={[r.status_code for r in results]}"
    replacement_csrf = _extract_csrf_token(credential_failures[0].text)
    replacement_cookie = credential_failures[0].cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert replacement_cookie

    replay = _login_post(
        client_a,
        csrf_token=replacement_csrf,
        cookies={LOGIN_FLOW_COOKIE_NAME: replacement_cookie},
        password="wrong-password",
    )
    assert replay.status_code == 401

    loser_replay = _login_post(
        client_b,
        csrf_token=csrf_token,
        cookies=cookies,
        password="wrong-password",
    )
    assert loser_replay.status_code == 400
    assert _count_sessions(pg_conn) == 0
    assert _count_login_success_audits(pg_conn) == 0


@pytest.mark.integration
def test_pg_route_claim_failure_writes_no_success_audit_or_session(
    pg_conn: psycopg.Connection,
) -> None:
    csrf_token, cookies = _fetch_pg_login_form()
    before_audits = _count_login_success_audits(pg_conn)

    with patch(
        "app.admin_routes.db.claim_admin_login_flow",
        side_effect=RuntimeError("database unavailable"),
    ):
        with patch("app.admin_auth.verify_admin_credentials") as verify:
            response = _login_post(client_a, csrf_token=csrf_token, cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    assert SESSION_COOKIE_NAME not in response.cookies
    assert LOGIN_FLOW_COOKIE_NAME not in response.cookies
    assert _count_sessions(pg_conn) == 0
    assert _count_login_success_audits(pg_conn) == before_audits


@pytest.mark.unit
def test_require_test_database_fails_closed_for_claim_race_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("REQUIRE_TEST_DATABASE", "1")
    with pytest.raises(Failed, match="TEST_DATABASE_URL is unset"):
        _require_database_url()


@pytest.mark.unit
def test_postgres_claim_race_tests_are_integration_marked() -> None:
    import tests.test_admin_login_flow_claim_pg_integration as module

    postgres_race_tests = [
        obj
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_pg_")
    ]
    assert postgres_race_tests
    for test_fn in postgres_race_tests:
        marks = {mark.name for mark in getattr(test_fn, "pytestmark", [])}
        assert "integration" in marks, f"{test_fn.__name__} missing integration marker"
