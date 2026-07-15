"""PostgreSQL integration tests for atomic admin login-flow claims (#243)."""

from __future__ import annotations

import inspect
import os
import threading
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

client = TestClient(app, follow_redirects=False)

TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"

pytestmark = [pytest.mark.integration]

_ADMITTED = admin_auth.LoginAdmissionResult(
    admitted=True,
    throttled=False,
    already_locked=False,
    lockout_transition=False,
)


@contextmanager
def _admit_all_login_attempts() -> Iterator[None]:
    """Bypass rate limiting without replacing real ``db.db_connection`` handles."""
    with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
        yield


@contextmanager
def _coordinate_claim_barrier(barrier: threading.Barrier) -> Iterator[None]:
    """Rendezvous workers on the real claim call without serializing the winner."""
    original = db.claim_admin_login_flow

    def coordinated(
        conn: psycopg.Connection,
        *,
        flow_token_hash: str,
        csrf_token_hash: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        barrier.wait(timeout=15)
        return original(
            conn,
            flow_token_hash=flow_token_hash,
            csrf_token_hash=csrf_token_hash,
            now=now,
        )

    with patch("app.admin_routes.db.claim_admin_login_flow", side_effect=coordinated):
        yield

_CLAIM_UPDATE_SQL = """
UPDATE admin_login_flows
SET consumed_at = %s
WHERE flow_token_hash = %s
  AND csrf_token_hash = %s
  AND consumed_at IS NULL
  AND expires_at > %s
RETURNING id, flow_token_hash, csrf_token_hash, created_at, expires_at, consumed_at
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
def postgres_app_env(database_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    admin_auth.reset_login_rate_limiter()
    return database_url


def _seed_valid_flow(
    conn: psycopg.Connection,
    *,
    now: datetime,
    expires_at: datetime | None = None,
    raw_csrf: str | None = None,
) -> tuple[int, str, str, str, str, datetime]:
    raw_flow = admin_auth.generate_session_token()
    raw_csrf_value = raw_csrf or admin_auth.generate_csrf_value()
    flow_hash = admin_auth.hash_session_token(raw_flow)
    csrf_hash = admin_auth.hash_csrf_token(raw_csrf_value)
    expiry = expires_at or (now + timedelta(minutes=15))
    flow_id = db.create_admin_login_flow(
        conn,
        flow_token_hash=flow_hash,
        csrf_token_hash=csrf_hash,
        expires_at=expiry,
    )
    return flow_id, raw_flow, raw_csrf_value, flow_hash, csrf_hash, expiry


def _fetch_flow_row(conn: psycopg.Connection, flow_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, consumed_at, expires_at
            FROM admin_login_flows
            WHERE id = %s
            """,
            (flow_id,),
        )
        return cur.fetchone()


def _count_flow_rows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_flows")
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _count_consumed_flows(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_flows WHERE consumed_at IS NOT NULL"
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _count_sessions(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_sessions")
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _count_login_success_audits(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM audit_events WHERE action = %s",
            (audit_service.ACTION_AUTH_LOGIN_SUCCESS,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row["count"])


def _fetch_live_flow() -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    assert response.status_code == 200
    return _parse_login_form(response)


def _login_post(
    *,
    csrf_token: str,
    cookies: dict[str, str],
    password: str = TEST_PASSWORD,
) -> Any:
    return client.post(
        "/admin/login",
        data={
            "username": TEST_USERNAME,
            "password": password,
            "csrf_token": csrf_token,
        },
        cookies=cookies,
    )


def _race_claims(
    *,
    database_url: str,
    flow_hash: str,
    csrf_hash: str,
    now: datetime,
    workers: int,
) -> list[dict[str, Any]]:
    barrier = threading.Barrier(workers, timeout=15)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(worker_id: int) -> None:
        try:
            barrier.wait(timeout=15)
            with _connect(database_url) as conn:
                row = db.claim_admin_login_flow(
                    conn,
                    flow_token_hash=flow_hash,
                    csrf_token_hash=csrf_hash,
                    now=now,
                )
            with guard:
                results.append(
                    {
                        "worker_id": worker_id,
                        "claimed": row is not None,
                        "flow_id": int(row["id"]) if row else None,
                    }
                )
        except BaseException as exc:  # pragma: no cover - threaded harness guard
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], f"worker errors: {errors!r}"
    return results


def test_repository_race_one_winner_one_zero_row(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    results = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=2,
    )

    winners = [result for result in results if result["claimed"]]
    losers = [result for result in results if not result["claimed"]]
    assert len(winners) == 1, f"unexpected claim results: {results}"
    assert len(losers) == 1, f"unexpected claim results: {results}"
    assert winners[0]["flow_id"] == flow_id

    final_row = _fetch_flow_row(pg_conn, flow_id)
    assert final_row is not None
    assert final_row["consumed_at"] is not None
    assert _count_consumed_flows(pg_conn) == 1
    assert _count_flow_rows(pg_conn) == 1


def test_repository_burst_three_or_more_claimants_one_winner(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    results = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=5,
    )

    winners = [result for result in results if result["claimed"]]
    losers = [result for result in results if not result["claimed"]]
    assert len(winners) == 1, f"unexpected claim results: {results}"
    assert len(losers) == 4, f"unexpected claim results: {results}"
    assert winners[0]["flow_id"] == flow_id
    assert _count_consumed_flows(pg_conn) == 1


def test_second_claimant_waits_for_row_lock_then_observes_zero_row(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    ready = threading.Barrier(2, timeout=15)
    holder_locked = threading.Event()
    claim_entered = threading.Event()
    claim_finished = threading.Event()
    waiter_result: dict[str, Any] = {}
    waiter_error: list[BaseException] = []

    def holder() -> None:
        conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
        try:
            ready.wait(timeout=15)
            with conn.cursor() as cur:
                cur.execute(_CLAIM_UPDATE_SQL, (now, flow_hash, csrf_hash, now))
                row = cur.fetchone()
            assert row is not None
            holder_locked.set()
            assert claim_entered.wait(timeout=15)
            conn.commit()
        finally:
            conn.close()

    def waiter() -> None:
        try:
            ready.wait(timeout=15)
            assert holder_locked.wait(timeout=15)
            claim_entered.set()
            with _connect(database_url) as conn:
                row = db.claim_admin_login_flow(
                    conn,
                    flow_token_hash=flow_hash,
                    csrf_token_hash=csrf_hash,
                    now=now,
                )
            waiter_result["claimed"] = row is not None
            waiter_result["flow_id"] = int(row["id"]) if row else None
        except BaseException as exc:  # pragma: no cover - threaded harness guard
            waiter_error.append(exc)
        finally:
            claim_finished.set()

    holder_thread = threading.Thread(target=holder)
    waiter_thread = threading.Thread(target=waiter)
    holder_thread.start()
    waiter_thread.start()
    holder_thread.join(timeout=30)
    waiter_thread.join(timeout=30)

    assert waiter_error == []
    assert claim_finished.is_set()
    assert waiter_result == {"claimed": False, "flow_id": None}
    final_row = _fetch_flow_row(pg_conn, flow_id)
    assert final_row is not None
    assert final_row["consumed_at"] is not None


def test_winner_rollback_leaves_flow_claimable(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    rolled_back = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        with rolled_back.cursor() as cur:
            cur.execute(_CLAIM_UPDATE_SQL, (now, flow_hash, csrf_hash, now))
            row = cur.fetchone()
        assert row is not None
        rolled_back.rollback()
    finally:
        rolled_back.close()

    after_rollback = _fetch_flow_row(pg_conn, flow_id)
    assert after_rollback is not None
    assert after_rollback["consumed_at"] is None

    with _connect(database_url) as conn:
        claimed = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=now,
        )

    assert claimed is not None
    assert int(claimed["id"]) == flow_id
    assert _count_consumed_flows(pg_conn) == 1


def test_exact_expiry_boundary_rejects_claim_at_or_after_expires_at(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    boundary = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn,
        now=boundary - timedelta(minutes=5),
        expires_at=boundary,
    )

    with _connect(database_url) as conn:
        at_boundary = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=boundary,
        )
        pre_boundary = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=boundary - timedelta(microseconds=1),
        )

    assert at_boundary is None
    assert pre_boundary is not None
    assert int(pre_boundary["id"]) == flow_id
    final_row = _fetch_flow_row(pg_conn, flow_id)
    assert final_row is not None
    assert final_row["consumed_at"] is not None


def test_wrong_csrf_under_concurrent_pressure_claims_nothing(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, _csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )
    wrong_hash = admin_auth.hash_csrf_token("wrong-csrf-token")

    results = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=wrong_hash,
        now=now,
        workers=2,
    )

    assert all(not result["claimed"] for result in results), f"results: {results}"
    final_row = _fetch_flow_row(pg_conn, flow_id)
    assert final_row is not None
    assert final_row["consumed_at"] is None


def test_cleanup_race_cannot_delete_active_flow_or_double_consume(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    cleanup_started = threading.Event()
    claim_may_proceed = threading.Event()
    claim_result: dict[str, Any] = {}

    def cleanup_worker() -> None:
        cleanup_started.wait(timeout=15)
        with _connect(database_url) as conn:
            deleted = db.cleanup_stale_admin_login_flows(
                conn,
                now=now,
                expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
                consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
                batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
            )
        claim_result["deleted"] = deleted

    def claim_worker() -> None:
        with _connect(database_url) as conn:
            cleanup_started.set()
            claim_may_proceed.wait(timeout=15)
            row = db.claim_admin_login_flow(
                conn,
                flow_token_hash=flow_hash,
                csrf_token_hash=csrf_hash,
                now=now,
            )
        claim_result["claimed"] = row is not None
        claim_result["flow_id"] = int(row["id"]) if row else None

    cleanup_thread = threading.Thread(target=cleanup_worker)
    claim_thread = threading.Thread(target=claim_worker)
    claim_thread.start()
    cleanup_thread.start()
    cleanup_thread.join(timeout=15)
    assert _fetch_flow_row(pg_conn, flow_id) is not None
    claim_may_proceed.set()
    claim_thread.join(timeout=15)

    assert claim_result.get("claimed") is True
    assert claim_result.get("flow_id") == flow_id
    assert claim_result.get("deleted", 0) == 0
    final_row = _fetch_flow_row(pg_conn, flow_id)
    assert final_row is not None
    assert final_row["consumed_at"] is not None


def test_route_race_with_valid_credentials_one_session_and_audit(
    pg_conn: psycopg.Connection,
    postgres_app_env: str,
) -> None:
    csrf_token, cookies = _fetch_live_flow()
    verify_calls: list[tuple[str, str]] = []
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=15)

    def worker() -> None:
        try:
            results.append(_login_post(csrf_token=csrf_token, cookies=cookies))
        except BaseException as exc:  # pragma: no cover - threaded harness guard
            errors.append(exc)

    with _admit_all_login_attempts():
        with _coordinate_claim_barrier(barrier):
            with patch(
                "app.admin_auth.verify_admin_credentials",
                side_effect=lambda user, pwd, settings: (
                    verify_calls.append((user, pwd)) or True
                ),
            ):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

    assert errors == []
    assert len(verify_calls) == 1
    successes = [response for response in results if response.status_code == 303]
    failures = [response for response in results if response.status_code != 303]
    assert len(successes) == 1, f"statuses={[response.status_code for response in results]}"
    assert len(failures) == 1, f"statuses={[response.status_code for response in results]}"
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in failures[0].text
    assert _extract_session_cookie(successes[0]) is not None
    assert _count_sessions(pg_conn) == 1
    assert _count_login_success_audits(pg_conn) == 1


def test_route_race_with_invalid_credentials_one_verify_and_replacement(
    pg_conn: psycopg.Connection,
    postgres_app_env: str,
) -> None:
    csrf_token, cookies = _fetch_live_flow()
    verify_calls = {"count": 0}
    results: list[Any] = []
    barrier = threading.Barrier(2, timeout=15)

    def worker() -> None:
        results.append(
            _login_post(csrf_token=csrf_token, cookies=cookies, password="wrong-password")
        )

    with _admit_all_login_attempts():
        with _coordinate_claim_barrier(barrier):
            with patch(
                "app.admin_auth.verify_admin_credentials",
                side_effect=lambda user, pwd, settings: verify_calls.__setitem__(
                    "count", verify_calls["count"] + 1
                )
                or False,
            ):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

    assert verify_calls["count"] == 1
    credential_failures = [response for response in results if response.status_code == 401]
    claim_failures = [response for response in results if response.status_code == 400]
    assert len(credential_failures) == 1
    assert len(claim_failures) == 1
    replacement_csrf = _extract_csrf_token(credential_failures[0].text)
    replacement_cookie = credential_failures[0].cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert replacement_cookie

    loser_replay = _login_post(
        csrf_token=csrf_token,
        cookies=cookies,
        password="wrong-password",
    )
    assert loser_replay.status_code == 400
    assert _count_sessions(pg_conn) == 0
    assert _count_login_success_audits(pg_conn) == 0

    replacement_replay = _login_post(
        csrf_token=replacement_csrf,
        cookies={LOGIN_FLOW_COOKIE_NAME: replacement_cookie},
        password="wrong-password",
    )
    assert replacement_replay.status_code == 401


def test_route_claim_database_failure_does_not_verify_or_mutate_session(
    pg_conn: psycopg.Connection,
    postgres_app_env: str,
) -> None:
    csrf_token, cookies = _fetch_live_flow()
    flow_cookie = cookies[LOGIN_FLOW_COOKIE_NAME]
    flow_hash = admin_auth.hash_session_token(flow_cookie)

    with _admit_all_login_attempts():
        with patch(
            "app.admin_routes.db.claim_admin_login_flow",
            side_effect=RuntimeError("database unavailable"),
        ):
            with patch("app.admin_auth.verify_admin_credentials") as verify:
                response = _login_post(csrf_token=csrf_token, cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    assert SESSION_COOKIE_NAME not in response.cookies
    assert _count_sessions(pg_conn) == 0
    assert _count_login_success_audits(pg_conn) == 0

    row = db.get_admin_login_flow_by_token_hash(pg_conn, flow_hash)
    assert row is not None
    assert row["consumed_at"] is None


def test_separate_repository_instances_share_one_postgres_winner(
    pg_conn: psycopg.Connection,
    database_url: str,
) -> None:
    now = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    flow_id, _raw_flow, _raw_csrf, flow_hash, csrf_hash, _expires_at = _seed_valid_flow(
        pg_conn, now=now
    )

    results = _race_claims(
        database_url=database_url,
        flow_hash=flow_hash,
        csrf_hash=csrf_hash,
        now=now,
        workers=2,
    )
    winners = [result for result in results if result["claimed"]]
    assert len(winners) == 1
    assert winners[0]["flow_id"] == flow_id


def test_require_database_url_fails_closed_when_ci_requires_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_TEST_DATABASE", "1")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(
        BaseException, match="REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset"
    ):
        _require_database_url()


def test_claim_integration_module_is_collected_as_integration_tests() -> None:
    import tests.test_admin_login_flow_claim_integration as claim_integration

    for name, obj in inspect.getmembers(claim_integration):
        if not name.startswith("test_") or not callable(obj):
            continue
        marks = getattr(obj, "pytestmark", [])
        mark_names = [
            mark.name for mark in marks if isinstance(mark, pytest.MarkDecorator)
        ]
        assert "integration" in mark_names or "integration" in {
            mark.name for mark in claim_integration.pytestmark
        }, name
