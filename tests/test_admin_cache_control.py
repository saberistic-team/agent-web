"""Integration tests for admin cache isolation (#337)."""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.responses import Response

from app import admin_auth
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_response_policy import apply_admin_cache_headers
from app.main import app

from tests.conftest import enable_admin_preview_env
from tests.test_admin_auth import (
    FakeRateLimitStore,
    LOGIN_FLOW_COOKIE_NAME,
    _extract_csrf_token,
    _extract_session_cookie,
    _fetch_login_form,
    _login,
    _login_flows,
    _session_store,
    mock_db_connection,
    shared_rate_limiter,
)
from tests.test_admin_security_headers import _assert_admin_cache_control

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


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
    _login_flows.clear()


@pytest.fixture
def preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch, base_url="http://localhost:8000")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")


def _login_success() -> Any:
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            return _login()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path", "status_code"),
    [
        ("GET", "/admin/login", 200),
        ("GET", "/admin", 303),
        ("GET", "/admin/briefs", 303),
    ],
)
def test_admin_response_matrix_has_single_cache_policy(
    method: str,
    path: str,
    status_code: int,
) -> None:
    if path == "/admin/login":
        with mock_db_connection():
            response = client.get(path)
    else:
        response = client.request(method, path)
    assert response.status_code == status_code
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_fastapi_validation_error_has_no_store() -> None:
    client.cookies.clear()
    response = client.post("/admin/login", data={})
    assert response.status_code == 422
    _assert_admin_cache_control(response)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "status_code"),
    [
        ("/admin/no-such-section", 404),
        ("/admin/briefs/503", 503),
    ],
)
def test_admin_preview_response_matrix_has_single_cache_policy(
    preview_mode: None,
    path: str,
    status_code: int,
) -> None:
    response = client.get(path)
    assert response.status_code == status_code
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_login_get_has_no_store() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_login_failed_post_has_no_store() -> None:
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
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_login_invalid_credentials_has_no_store() -> None:
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            response = _login(password="not-the-password")
    assert response.status_code == 401
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_login_success_redirect_has_no_store() -> None:
    response = _login_success()
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_logout_redirect_has_no_store() -> None:
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            login = _login()
            session_cookie = _extract_session_cookie(login)
            assert session_cookie is not None
            dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
            assert dashboard.status_code == 200
            logout_csrf = _extract_csrf_token(dashboard.text)
            response = client.post(
                "/admin/logout",
                data={"csrf_token": logout_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_authenticated_admin_html_has_no_store(preview_mode: None) -> None:
    response = client.get("/admin/briefs")
    assert response.status_code == 200
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_authenticated_admin_json_has_no_store() -> None:
    login = _login_success()
    session_cookie = _extract_session_cookie(login)
    assert session_cookie is not None
    with mock_db_connection():
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            cookies={SESSION_COOKIE_NAME: session_cookie},
            json={"batch_label": "test", "rows": []},
        )
    assert response.status_code in {400, 422, 503}
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_rate_limit_429_has_no_store() -> None:
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
    _assert_admin_cache_control(blocked)


@pytest.mark.integration
def test_admin_unconfigured_503_has_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    response = client.get("/admin/login")
    assert response.status_code == 503
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_unhandled_exception_500_has_no_store(preview_mode: None) -> None:
    with mock_db_connection():
        with patch(
            "app.admin_preview.build_preview_acquisition_dashboard_data",
            side_effect=RuntimeError("test failure"),
        ):
            response = client.get("/admin")
    assert response.status_code == 500
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_preview_guard_405_has_no_store(preview_mode: None) -> None:
    response = client.post(
        "/admin/pipeline/not-a-uuid/stage",
        data={"stage": "qualifying", "csrf_token": "bad"},
    )
    assert response.status_code == 405
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_static_assets_keep_intended_cache_behavior() -> None:
    response = client.get("/assets/admin.css", headers={"Cookie": f"{SESSION_COOKIE_NAME}=abc"})
    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert response.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.integration
def test_public_pages_remain_unchanged() -> None:
    response = client.get("/", headers={"Cookie": f"{SESSION_COOKIE_NAME}=abc"})
    assert response.status_code == 200
    assert "cache-control" not in response.headers


@pytest.mark.integration
def test_handler_cache_control_cannot_weaken_admin_policy() -> None:
    response = Response(content="admin", status_code=200)
    response.headers["Cache-Control"] = "public, max-age=3600"
    apply_admin_cache_headers(response)
    _assert_admin_cache_control(response)


@pytest.mark.integration
def test_admin_cache_control_header_appears_once(preview_mode: None) -> None:
    response = client.get("/admin/login")
    names = [name.lower() for name, _ in response.headers.multi_items()]
    assert Counter(names)["cache-control"] == 1


@pytest.mark.integration
def test_admin_login_required_redirect_has_no_store() -> None:
    response = client.get("/admin/companies")
    assert response.status_code == 303
    _assert_admin_cache_control(response)
