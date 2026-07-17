"""Integration tests for admin cache isolation (#337)."""

from __future__ import annotations

from typing import Any, Generator
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException

from tests.test_admin_auth import (
    FakeRateLimitStore,
    SESSION_COOKIE_NAME,
    _login_flows,
    _session_store,
    client,
    mock_db_connection,
    shared_rate_limiter,
)
from app import admin_auth
from tests.test_admin_security_headers import _assert_admin_cache_headers
from tests.conftest import enable_admin_preview_env

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
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _login_success() -> Any:
    from tests.test_admin_auth import _login

    with mock_db_connection():
        return _login()


@pytest.fixture
def rate_limit_store() -> Generator[FakeRateLimitStore, None, None]:
    yield FakeRateLimitStore()


@pytest.mark.integration
def test_login_get_has_no_store() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_login_failed_post_has_no_store() -> None:
    from tests.test_admin_auth import _fetch_login_form

    with mock_db_connection():
        csrf_token, cookies = _fetch_login_form()
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
    assert response.status_code == 401
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_login_success_redirect_has_no_store(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        response = _login_success()
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_logout_redirect_has_no_store(rate_limit_store: FakeRateLimitStore) -> None:
    from tests.test_admin_auth import _extract_csrf_token

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            login = _login_success()
            session_cookie = login.cookies.get(SESSION_COOKIE_NAME)
            assert session_cookie

            dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
            assert dashboard.status_code == 200
            _assert_admin_cache_headers(dashboard)

            logout_csrf = _extract_csrf_token(dashboard.text)
            logout = client.post(
                "/admin/logout",
                data={"csrf_token": logout_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    _assert_admin_cache_headers(logout)


@pytest.mark.integration
def test_authenticated_json_has_no_store(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            login = _login_success()
            session_cookie = login.cookies.get(SESSION_COOKIE_NAME)
            assert session_cookie
            response = client.post(
                "/admin/api/imports/linkedin/commit",
                cookies={SESSION_COOKIE_NAME: session_cookie},
                json={"batch_label": "test", "rows": []},
            )
    assert response.status_code in {400, 422, 503}
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_500_handler_has_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch, base_url="http://localhost:8000")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    with patch(
        "app.admin_dashboard_pages.render_acquisition_dashboard_page",
        side_effect=HTTPException(status_code=500, detail="simulated failure"),
    ):
        response = client.get("/admin")
    assert response.status_code == 500
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_401_login_failure_has_no_store() -> None:
    from tests.test_admin_auth import _fetch_login_form

    with mock_db_connection():
        csrf_token, cookies = _fetch_login_form()
        response = client.post(
            "/admin/login",
            data={
                "username": "nobody",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
    assert response.status_code == 401
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_static_asset_with_admin_cookie_keeps_intended_cache(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    from app.admin_response_policy import ADMIN_CACHE_CONTROL

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            login = _login_success()
            session_cookie = login.cookies.get(SESSION_COOKIE_NAME)
            assert session_cookie
            response = client.get(
                "/assets/admin.css",
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control")
    if cache_control is not None:
        assert cache_control != ADMIN_CACHE_CONTROL
    assert "no-store" not in (cache_control or "").lower()
