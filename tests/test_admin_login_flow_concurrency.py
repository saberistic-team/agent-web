"""Concurrent admin login-flow claim tests (#216)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_PASSWORD,
    TEST_USERNAME,
    _extract_csrf_token,
    _extract_session_cookie,
    _login_flows,
    _mock_claim_admin_login_flow,
    _mock_cleanup_stale_admin_login_flows,
    _mock_create_admin_login_flow,
    _parse_login_form,
    _session_store,
    mock_db_connection,
    shared_rate_limiter,
)

pytestmark = [pytest.mark.integration]

client_a = TestClient(app, follow_redirects=False)
client_b = TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror ``test_admin_auth.admin_env`` so module-local tests share credentials."""
    from tests.test_admin_auth import TEST_HASH, TEST_SECRET, TEST_USERNAME

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _fetch_login_form(test_client: TestClient = client_a) -> tuple[str, dict[str, str]]:
    with mock_db_connection():
        response = test_client.get("/admin/login")
    return _parse_login_form(response)


def _concurrent_login_posts(
    *,
    csrf_token: str,
    cookies: dict[str, str],
    password: str = TEST_PASSWORD,
    claim_side_effect: Callable[..., Any] | None = None,
    claim_pause: threading.Event | None = None,
) -> list[Any]:
    """POST the same flow from two app instances with a startup barrier."""

    barrier = threading.Barrier(2)
    results: list[Any] = []
    results_lock = threading.Lock()

    def _worker(test_client: TestClient) -> None:
        barrier.wait()
        side_effect = claim_side_effect
        if claim_pause is not None:
            side_effect = _claim_with_pause(claim_pause)
        with ExitStack() as stack:
            stack.enter_context(mock_db_connection())
            if side_effect is not None:
                stack.enter_context(
                    patch(
                        "app.admin_routes.db.claim_admin_login_flow",
                        side_effect=side_effect,
                    )
                )
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

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_worker, client_a)
        pool.submit(_worker, client_b)
    return results


def _claim_with_pause(pause: threading.Event):
    def _claim(conn: MagicMock, **kwargs: Any) -> dict[str, Any] | None:
        pause.wait(timeout=5)
        return _mock_claim_admin_login_flow(conn, **kwargs)

    return _claim


@pytest.mark.unit
def test_claim_admin_login_flow_sql_is_conditional_update() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"id": 1}
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    from app import db

    row = db.claim_admin_login_flow(
        conn,
        flow_token_hash="flow-hash",
        csrf_token_hash="csrf-hash",
        now=now,
    )

    sql = cur.execute.call_args.args[0]
    assert "UPDATE admin_login_flows" in sql
    assert "csrf_token_hash = %s" in sql
    assert "consumed_at IS NULL" in sql
    assert "expires_at > %s" in sql
    assert "RETURNING" in sql
    assert row == {"id": 1}
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_consume_admin_login_flow_returns_false_on_zero_row_update() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    from app import db

    claimed = db.consume_admin_login_flow(
        conn,
        flow_token_hash="missing-flow",
        now=now,
    )
    assert claimed is False


@pytest.mark.unit
def test_concurrent_replay_valid_credentials_one_password_verify_and_session() -> None:
    verify_calls = {"count": 0}
    verify_lock = threading.Lock()
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(*args: Any, **kwargs: Any) -> bool:
        with verify_lock:
            verify_calls["count"] += 1
        return original_verify(*args, **kwargs)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            with patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=counting_verify,
            ):
                responses = _concurrent_login_posts(csrf_token=csrf_token, cookies=cookies)

    status_codes = sorted(response.status_code for response in responses)
    assert status_codes == [303, 400]
    assert verify_calls["count"] == 1
    assert len(_session_store) == 1
    successes = [response for response in responses if response.status_code == 303]
    assert _extract_session_cookie(successes[0])


@pytest.mark.unit
def test_concurrent_replay_invalid_credentials_one_verify_and_replacement_isolated() -> None:
    verify_calls = {"count": 0}
    verify_lock = threading.Lock()
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(*args: Any, **kwargs: Any) -> bool:
        with verify_lock:
            verify_calls["count"] += 1
        return original_verify(*args, **kwargs)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            with patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=counting_verify,
            ):
                responses = _concurrent_login_posts(
                    csrf_token=csrf_token,
                    cookies=cookies,
                    password="wrong-password",
                )

    status_codes = sorted(response.status_code for response in responses)
    assert status_codes == [400, 401]
    assert verify_calls["count"] == 1

    winner = next(response for response in responses if response.status_code == 401)
    loser = next(response for response in responses if response.status_code == 400)
    winner_csrf = _extract_csrf_token(winner.text)
    winner_cookies = {LOGIN_FLOW_COOKIE_NAME: winner.cookies[LOGIN_FLOW_COOKIE_NAME]}

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            replay_original = client_a.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            winner_retry = client_a.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": winner_csrf,
                },
                cookies=winner_cookies,
            )

    assert replay_original.status_code == 400
    assert winner_retry.status_code == 303
    assert loser.status_code == 400
    assert winner_cookies[LOGIN_FLOW_COOKIE_NAME] != cookies[LOGIN_FLOW_COOKIE_NAME]


@pytest.mark.unit
def test_concurrent_replay_uses_distinct_connections() -> None:
    thread_ids: list[int] = []
    thread_lock = threading.Lock()

    def track_claim(conn: MagicMock, **kwargs: Any) -> dict[str, Any] | None:
        with thread_lock:
            thread_ids.append(threading.current_thread().ident)
        return _mock_claim_admin_login_flow(conn, **kwargs)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            responses = _concurrent_login_posts(
                csrf_token=csrf_token,
                cookies=cookies,
                claim_side_effect=track_claim,
            )

    assert len(thread_ids) == 2
    assert len(set(thread_ids)) == 2
    assert sorted(response.status_code for response in responses) == [303, 400]


@pytest.mark.unit
def test_zero_row_claim_stops_login_before_password_verify() -> None:
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            with (
                patch("app.admin_routes.db.claim_admin_login_flow", return_value=None),
                patch(
                    "app.admin_routes.admin_auth.verify_admin_credentials"
                ) as verify_credentials,
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
    verify_credentials.assert_not_called()
    assert response.status_code == 400
    assert SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.unit
def test_expiry_boundary_claim_fails_at_exact_expiry() -> None:
    boundary = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            with patch(
                "app.admin_routes.datetime",
                wraps=datetime,
            ) as route_datetime:
                route_datetime.now.return_value = boundary
                csrf_token, cookies = _fetch_login_form()
                flow_cookie = cookies[LOGIN_FLOW_COOKIE_NAME]
                flow_hash = admin_auth.hash_session_token(flow_cookie)
                _login_flows[flow_hash]["expires_at"] = boundary

                response = client_a.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )

    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
def test_wrong_browser_and_csrf_cannot_claim_flow() -> None:
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_a, cookies_a = _fetch_login_form(client_a)
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_b, cookies_b = _fetch_login_form(client_b)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            cross_browser = client_a.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_a,
                },
                cookies=cookies_b,
            )
            wrong_csrf = client_a.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "not-the-flow-token",
                },
                cookies=cookies_a,
            )

    assert cross_browser.status_code == 400
    assert wrong_csrf.status_code == 400
    assert len(_session_store) == 0


@pytest.mark.unit
def test_claim_database_failure_skips_password_verify_and_session_mutation() -> None:
    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            with (
                patch(
                    "app.admin_routes.db.claim_admin_login_flow",
                    side_effect=RuntimeError("database unavailable"),
                ),
                patch(
                    "app.admin_routes.admin_auth.verify_admin_credentials"
                ) as verify_credentials,
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

    verify_credentials.assert_not_called()
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.unit
def test_cleanup_does_not_delete_in_flight_flow_during_claim() -> None:
    claim_started = threading.Event()
    claim_release = threading.Event()
    cleanup_ran = threading.Event()
    response_holder: dict[str, Any] = {}

    def paused_claim(conn: MagicMock, **kwargs: Any) -> dict[str, Any] | None:
        claim_started.set()
        claim_release.wait(timeout=5)
        return _mock_claim_admin_login_flow(conn, **kwargs)

    def tracked_cleanup(conn: MagicMock, **kwargs: Any) -> int:
        cleanup_ran.set()
        return _mock_cleanup_stale_admin_login_flows(conn, **kwargs)

    with shared_rate_limiter(FakeRateLimitStore()):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            flow_hash = admin_auth.hash_session_token(cookies[LOGIN_FLOW_COOKIE_NAME])

            with (
                patch("app.admin_routes.db.claim_admin_login_flow", side_effect=paused_claim),
                patch(
                    "app.admin_routes.db.cleanup_stale_admin_login_flows",
                    side_effect=tracked_cleanup,
                ),
            ):
                worker = threading.Thread(
                    target=lambda: response_holder.update(
                        {
                            "response": client_a.post(
                                "/admin/login",
                                data={
                                    "username": TEST_USERNAME,
                                    "password": TEST_PASSWORD,
                                    "csrf_token": csrf_token,
                                },
                                cookies=cookies,
                            )
                        }
                    ),
                )
                worker.start()
                assert claim_started.wait(timeout=5)

                mint = client_b.get("/admin/login")

                assert cleanup_ran.wait(timeout=5)
                assert flow_hash in _login_flows
                claim_release.set()
                worker.join(timeout=5)

    assert mint.status_code == 200
    assert worker.is_alive() is False
    assert response_holder["response"].status_code == 303
