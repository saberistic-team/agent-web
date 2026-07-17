"""Integration tests for admin cache isolation on the response matrix (#337)."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_cache_policy import ADMIN_CACHE_CONTROL
from app.main import app

from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    admin_auth.reset_login_rate_limiter()
    _session_store.clear()
    from tests.test_admin_auth import client as auth_client

    auth_client.cookies.clear()
    client.cookies.clear()


def _header_values(headers: Any, name: str) -> list[str]:
    raw = headers.get_list(name) if hasattr(headers, "get_list") else [headers.get(name)]
    return [value for value in raw if value is not None]


def _assert_admin_cache_headers(response: Any) -> None:
    values = _header_values(response.headers, "cache-control")
    assert len(values) == 1, f"cache-control must appear once, got {values}"
    assert values[0] == ADMIN_CACHE_CONTROL


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
    }


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, str], None, None]:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(token_hash=token_hash)

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        yield {SESSION_COOKIE_NAME: raw_token}


@pytest.fixture
def preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch, base_url="http://localhost:8000")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")


@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    [
        "/admin/login",
        "/admin",
        "/admin/briefs",
        "/admin/companies",
        "/admin/contacts",
        "/admin/pipeline",
        "/admin/imports",
        "/admin/audit",
    ],
)
def test_admin_html_pages_emit_cache_headers(
    preview_mode: None,
    path: str,
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_redirect_to_login_has_cache_headers() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    _assert_admin_cache_headers(response)
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.integration
def test_admin_login_required_exception_redirect_has_cache_headers() -> None:
    response = client.get("/admin/briefs")
    assert response.status_code == 303
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_json_commit_has_cache_headers(authenticated_admin: dict[str, str]) -> None:
    with mock_db_connection():
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            cookies=authenticated_admin,
            json={"batch_label": "test", "rows": []},
        )
    assert response.status_code in {400, 422, 503}
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_fastapi_validation_error_has_cache_headers() -> None:
    response = client.post("/admin/login", data={})
    assert response.status_code == 422
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_validation_failure_has_cache_headers() -> None:
    with mock_db_connection() as conn:
        conn.execute = MagicMock()
        with patch("app.admin_routes.db.create_admin_login_flow"):
            with patch("app.admin_routes.db.cleanup_stale_admin_login_flows"):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "x",
                        "password": "y",
                        "csrf_token": "bad-token",
                    },
                )
    assert response.status_code == 400
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_404_shell_has_cache_headers(preview_mode: None) -> None:
    response = client.get("/admin/no-such-section")
    assert response.status_code == 404
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_503_preview_fixture_has_cache_headers(preview_mode: None) -> None:
    response = client.get("/admin/briefs/503")
    assert response.status_code == 503
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_pipeline_json_error_has_cache_headers(
    preview_mode: None,
) -> None:
    response = client.post(
        "/admin/pipeline/not-a-uuid/stage",
        data={"stage": "qualifying", "csrf_token": "bad"},
    )
    assert response.status_code == 405
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_unhandled_exception_has_cache_headers(preview_mode: None) -> None:
    with patch(
        "app.admin_dashboard_pages.render_acquisition_dashboard_page",
        side_effect=RuntimeError("boom"),
    ):
        response = client.get("/admin")
    assert response.status_code == 500
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_rate_limit_429_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        LOGIN_FLOW_COOKIE_NAME,
        _extract_csrf_token,
        _fetch_login_form,
        shared_rate_limiter,
    )

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            for _ in range(5):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401
                csrf_token = _extract_csrf_token(response.text)
                flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
                if flow_cookie:
                    cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
            blocked = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert blocked.status_code == 429
    _assert_admin_cache_headers(blocked)


@pytest.mark.integration
def test_login_get_has_cache_headers() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_login_failed_post_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        mock_db_connection as auth_mock_db,
        shared_rate_limiter,
        _login,
    )

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with auth_mock_db():
            response = _login(password="wrong-password")
    assert response.status_code == 401
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_login_success_redirect_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        mock_db_connection as auth_mock_db,
        shared_rate_limiter,
        _login,
    )

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with auth_mock_db():
            response = _login()
    assert response.status_code == 303
    _assert_admin_cache_headers(response)
    assert response.headers["location"] == "/admin"


@pytest.mark.integration
def test_logout_redirect_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        SESSION_COOKIE_NAME,
        mock_db_connection as auth_mock_db,
        shared_rate_limiter,
        _extract_csrf_token,
        _extract_session_cookie,
        _login,
    )

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        login = _login()
        assert login.status_code == 303
        session_cookie = _extract_session_cookie(login)
        with auth_mock_db():
            dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        logout_csrf = _extract_csrf_token(dashboard.text)
        with auth_mock_db():
            response = client.post(
                "/admin/logout",
                data={"csrf_token": logout_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert response.status_code == 303
    _assert_admin_cache_headers(response)
    assert response.headers["location"] == "/admin/login"


@pytest.mark.integration
def test_authenticated_html_has_cache_headers(authenticated_admin: dict[str, str]) -> None:
    with mock_db_connection():
        response = client.get("/admin/briefs", cookies=authenticated_admin)
    assert response.status_code == 200
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_authenticated_json_has_cache_headers(authenticated_admin: dict[str, str]) -> None:
    with mock_db_connection():
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            cookies=authenticated_admin,
            json={"batch_label": "test", "rows": []},
        )
    assert response.status_code in {400, 422, 503}
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_static_assets_keep_intended_cache_policy_without_no_store() -> None:
    response = client.get(
        "/assets/admin.css",
        cookies={SESSION_COOKIE_NAME: "session-token"},
        headers={"Referer": "http://testserver/admin"},
    )
    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert response.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.integration
def test_public_home_has_no_admin_cache_policy() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "cache-control" not in response.headers


@pytest.mark.integration
def test_admin_cache_control_appears_once(preview_mode: None) -> None:
    response = client.get("/admin/login")
    names = [name.lower() for name, _ in response.headers.multi_items()]
    counts = Counter(names)
    assert counts["cache-control"] == 1
    assert response.headers["cache-control"] == ADMIN_CACHE_CONTROL


@pytest.mark.integration
def test_admin_cache_headers_replace_weaker_downstream_directive() -> None:
    import app.admin_routes as admin_routes

    original_issue = admin_routes._issue_login_flow_response

    def _issue_with_weak_cache(**kwargs: Any) -> Any:
        response = original_issue(**kwargs)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    with (
        patch.object(admin_routes, "_issue_login_flow_response", side_effect=_issue_with_weak_cache),
        mock_db_connection(),
    ):
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_admin_cache_headers(response)
