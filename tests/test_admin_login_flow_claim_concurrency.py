"""Concurrent admin login-flow claim tests (issue #216)."""

from __future__ import annotations

pytest_plugins = ("tests.test_admin_auth",)

import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.main import app
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    _extract_csrf_token,
    _extract_session_cookie,
    _login_flows,
    _mock_claim_admin_login_flow,
    _mock_cleanup_stale_admin_login_flows,
    _mock_create_admin_session,
    _parse_login_form,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

pytestmark = [pytest.mark.unit, pytest.mark.integration]


class _ClaimBarrier:
    """Coordinate two threads so both attempt a flow claim concurrently."""

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2, timeout=5)
        self._lock = threading.Lock()

    def wait(self) -> None:
        self._barrier.wait(timeout=5)

    @contextmanager
    def coordinated_claim(
        self,
    ) -> Generator[None, None, None]:
        original = _mock_claim_admin_login_flow

        def coordinated(
            conn: MagicMock,
            *,
            flow_token_hash: str,
            csrf_token_hash: str,
            now: datetime,
        ) -> Any:
            self._barrier.wait(timeout=5)
            with self._lock:
                return original(
                    conn,
                    flow_token_hash=flow_token_hash,
                    csrf_token_hash=csrf_token_hash,
                    now=now,
                )

        with patch("app.admin_routes.db.claim_admin_login_flow", side_effect=coordinated):
            yield


@contextmanager
def _separate_connection_mock_db() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Two distinct connection handles sharing one in-memory login-flow store."""
    conn_a = MagicMock(name="conn_a")
    conn_b = MagicMock(name="conn_b")
    connections = iter((conn_a, conn_b))

    @contextmanager
    def rotating_connection(database_url: str | None) -> Generator[MagicMock, None, None]:
        yield next(connections, conn_a)

    with mock_db_connection() as _:
        with patch("app.admin_routes.db.db_connection", side_effect=rotating_connection):
            yield conn_a, conn_b


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


def _fetch_flow() -> tuple[str, dict[str, str]]:
    with mock_db_connection():
        response = client.get("/admin/login")
    return _parse_login_form(response)


def test_concurrent_replay_with_valid_credentials_one_session(
    rate_limit_store: Any,
) -> None:
    """Exactly one concurrent submission may verify credentials and mint a session."""
    csrf_token, cookies = _fetch_flow()
    verify_calls: list[tuple[str, str]] = []
    session_creates = {"count": 0}
    barrier = _ClaimBarrier()
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(_login_post(csrf_token=csrf_token, cookies=cookies))
        except BaseException as exc:  # pragma: no cover - threaded harness guard
            errors.append(exc)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_auth.verify_admin_credentials",
                side_effect=lambda user, pwd, settings: (
                    verify_calls.append((user, pwd)) or True
                ),
            ):
                with patch(
                    "app.admin_routes.db.create_admin_session",
                    side_effect=lambda conn, **kwargs: (
                        session_creates.__setitem__("count", session_creates["count"] + 1)
                        or _mock_create_admin_session(conn, **kwargs)
                    ),
                ):
                    with barrier.coordinated_claim():
                        threads = [threading.Thread(target=worker) for _ in range(2)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=10)

    assert not errors
    assert len(verify_calls) == 1
    assert session_creates["count"] == 1
    successes = [response for response in results if response.status_code == 303]
    failures = [response for response in results if response.status_code != 303]
    assert len(successes) == 1
    assert len(failures) == 1
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in failures[0].text
    assert _extract_session_cookie(successes[0])


def test_concurrent_replay_with_invalid_credentials_one_verify_and_replacement(
    rate_limit_store: Any,
) -> None:
    """One loser cannot replay the winner's replacement flow."""
    csrf_token, cookies = _fetch_flow()
    verify_calls = {"count": 0}
    barrier = _ClaimBarrier()
    results: list[Any] = []

    def worker() -> None:
        results.append(
            _login_post(csrf_token=csrf_token, cookies=cookies, password="wrong-password")
        )

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_auth.verify_admin_credentials",
                side_effect=lambda user, pwd, settings: verify_calls.__setitem__(
                    "count", verify_calls["count"] + 1
                )
                or False,
            ):
                with barrier.coordinated_claim():
                    threads = [threading.Thread(target=worker) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)

    assert verify_calls["count"] == 1
    credential_failures = [response for response in results if response.status_code == 401]
    claim_failures = [response for response in results if response.status_code == 400]
    assert len(credential_failures) == 1
    assert len(claim_failures) == 1
    replacement_csrf = _extract_csrf_token(credential_failures[0].text)
    replacement_cookie = credential_failures[0].cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert replacement_cookie

    with mock_db_connection():
        replay = _login_post(
            csrf_token=replacement_csrf,
            cookies={LOGIN_FLOW_COOKIE_NAME: replacement_cookie},
            password="wrong-password",
        )
        assert replay.status_code == 401

        loser_replay = _login_post(
            csrf_token=csrf_token, cookies=cookies, password="wrong-password"
        )
        assert loser_replay.status_code == 400


def test_concurrent_claim_uses_distinct_connection_handles(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()
    verify_calls = {"count": 0}
    barrier = _ClaimBarrier()
    seen_connections: list[int] = []

    def tracking_claim(
        conn: MagicMock,
        *,
        flow_token_hash: str,
        csrf_token_hash: str,
        now: datetime,
    ) -> Any:
        seen_connections.append(id(conn))
        barrier.wait()
        return _mock_claim_admin_login_flow(
            conn,
            flow_token_hash=flow_token_hash,
            csrf_token_hash=csrf_token_hash,
            now=now,
        )

    def worker() -> None:
        _login_post(csrf_token=csrf_token, cookies=cookies)

    with shared_rate_limiter(rate_limit_store):
        with _separate_connection_mock_db():
            with patch(
                "app.admin_auth.verify_admin_credentials",
                side_effect=lambda user, pwd, settings: verify_calls.__setitem__(
                    "count", verify_calls["count"] + 1
                )
                or True,
            ):
                with patch(
                    "app.admin_routes.db.claim_admin_login_flow",
                    side_effect=tracking_claim,
                ):
                    threads = [threading.Thread(target=worker) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)

    assert verify_calls["count"] == 1
    assert len(seen_connections) == 2
    assert len(set(seen_connections)) == 2


def test_zero_row_claim_stops_login_path(rate_limit_store: Any) -> None:
    csrf_token, cookies = _fetch_flow()

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.db.claim_admin_login_flow", return_value=None):
                with patch("app.admin_auth.verify_admin_credentials") as verify:
                    response = _login_post(csrf_token=csrf_token, cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_expiry_boundary_claim_fails_at_exact_expiry() -> None:
    flow_hash = admin_auth.hash_session_token("boundary-flow-token")
    boundary = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _login_flows[flow_hash] = {
        "id": 1,
        "flow_token_hash": flow_hash,
        "csrf_token_hash": "csrf",
        "created_at": boundary - timedelta(minutes=5),
        "expires_at": boundary,
        "consumed_at": None,
    }

    claimed = _mock_claim_admin_login_flow(
        MagicMock(),
        flow_token_hash=flow_hash,
        csrf_token_hash="csrf",
        now=boundary,
    )

    assert claimed is None
    assert _login_flows[flow_hash]["consumed_at"] is None


def test_expiry_boundary_login_rejects_without_password_verification(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()
    flow_hash = admin_auth.hash_session_token(cookies[LOGIN_FLOW_COOKIE_NAME])
    boundary = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _login_flows[flow_hash]["expires_at"] = boundary

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes._try_claim_login_flow",
                return_value=None,
            ):
                with patch("app.admin_auth.verify_admin_credentials") as verify:
                    response = _login_post(csrf_token=csrf_token, cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 400


def test_wrong_browser_cookie_cannot_claim_flow(rate_limit_store: Any) -> None:
    csrf_token, _cookies_a = _fetch_flow()
    _csrf_b, cookies_b = _fetch_flow()

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_auth.verify_admin_credentials") as verify:
                response = _login_post(csrf_token=csrf_token, cookies=cookies_b)

    verify.assert_not_called()
    assert response.status_code == 400


def test_wrong_csrf_after_successful_claim_issues_replacement(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_auth.verify_admin_credentials") as verify:
                response = _login_post(csrf_token="not-the-flow-token", cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 400
    assert response.cookies.get(LOGIN_FLOW_COOKIE_NAME)


def test_claim_database_failure_does_not_verify_or_set_session_cookie(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
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
    assert LOGIN_FLOW_COOKIE_NAME not in response.cookies


def test_rate_limit_denies_without_claim_or_verify(
    rate_limit_store: Any,
) -> None:
    from app.admin_auth import LoginAdmissionResult

    csrf_token, cookies = _fetch_flow()
    denied = LoginAdmissionResult(
        admitted=False,
        throttled=True,
        already_locked=True,
        lockout_transition=False,
    )

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_auth.try_admit_login_attempt",
                return_value=denied,
            ):
                with patch("app.admin_routes.db.claim_admin_login_flow") as claim:
                    with patch("app.admin_auth.verify_admin_credentials") as verify:
                        response = _login_post(csrf_token=csrf_token, cookies=cookies)

    claim.assert_not_called()
    verify.assert_not_called()
    assert response.status_code == 429
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_failed_claim_burn_failure_redirects_without_verify(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.db.claim_admin_login_flow", return_value=None):
                with patch(
                    "app.admin_routes._try_burn_login_flow_cookie",
                    side_effect=RuntimeError("burn failed"),
                ):
                    with patch("app.admin_auth.verify_admin_credentials") as verify:
                        response = _login_post(csrf_token=csrf_token, cookies=cookies)

    verify.assert_not_called()
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    assert SESSION_COOKIE_NAME not in response.cookies


def test_cleanup_during_concurrent_claim_cannot_delete_active_flow(
    rate_limit_store: Any,
) -> None:
    csrf_token, cookies = _fetch_flow()
    flow_hash = admin_auth.hash_session_token(cookies[LOGIN_FLOW_COOKIE_NAME])
    cleanup_started = threading.Event()
    claim_may_proceed = threading.Event()
    verify_calls = {"count": 0}

    def slow_claim(
        conn: MagicMock,
        *,
        flow_token_hash: str,
        csrf_token_hash: str,
        now: datetime,
    ) -> Any:
        cleanup_started.set()
        claim_may_proceed.wait(timeout=5)
        return _mock_claim_admin_login_flow(
            conn,
            flow_token_hash=flow_token_hash,
            csrf_token_hash=csrf_token_hash,
            now=now,
        )

    def cleanup_worker() -> None:
        cleanup_started.wait(timeout=5)
        _mock_cleanup_stale_admin_login_flows(
            MagicMock(),
            now=datetime.now(timezone.utc),
            expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
            consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
            batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
        )

    def login_worker() -> None:
        _login_post(csrf_token=csrf_token, cookies=cookies)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.db.claim_admin_login_flow",
                side_effect=slow_claim,
            ):
                with patch(
                    "app.admin_auth.verify_admin_credentials",
                    side_effect=lambda user, pwd, settings: verify_calls.__setitem__(
                        "count", verify_calls["count"] + 1
                    )
                    or True,
                ):
                    login_thread = threading.Thread(target=login_worker)
                    cleanup_thread = threading.Thread(target=cleanup_worker)
                    login_thread.start()
                    cleanup_thread.start()
                    cleanup_thread.join(timeout=5)
                    assert flow_hash in _login_flows
                    claim_may_proceed.set()
                    login_thread.join(timeout=10)

    assert verify_calls["count"] == 1
    assert _login_flows[flow_hash]["consumed_at"] is not None


def test_claim_admin_login_flow_sql_unit() -> None:
    from app import db

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"id": 1}
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

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
    assert cur.execute.call_args.args[1] == (now, "flow-hash", "csrf-hash", now)
    assert row == {"id": 1}
    conn.commit.assert_called_once()
