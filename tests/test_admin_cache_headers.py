"""Integration tests for admin cache isolation headers (#337)."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from fastapi import HTTPException

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_response_policy import ADMIN_CACHE_CONTROL, apply_admin_cache_headers
from app.main import app

from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

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
    client.cookies.clear()
    _session_store.clear()


def _header_values(headers: Any, name: str) -> list[str]:
    raw = headers.get_list(name) if hasattr(headers, "get_list") else [headers.get(name)]
    return [value for value in raw if value is not None]


def _assert_admin_cache_headers(response: Any) -> None:
    values = _header_values(response.headers, "cache-control")
    assert len(values) == 1, f"Cache-Control must appear once, got {values}"
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
def test_admin_login_invalid_credentials_has_cache_headers() -> None:
    with mock_db_connection():
        with patch("app.admin_routes.db.create_admin_login_flow"):
            with patch("app.admin_routes.db.cleanup_stale_admin_login_flows"):
                form = client.get("/admin/login")
                csrf_match = __import__("re").search(
                    r'name="csrf_token" value="([^"]+)"',
                    form.text,
                )
                assert csrf_match
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": csrf_match.group(1),
                    },
                    cookies=form.cookies,
                )
    assert response.status_code == 401
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
def test_admin_login_success_redirect_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _parse_login_form,
        mock_db_connection as auth_mock_db,
        shared_rate_limiter,
    )

    with shared_rate_limiter(FakeRateLimitStore()):
        with auth_mock_db():
            form = client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert response.status_code == 303
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_logout_redirect_has_cache_headers() -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        SESSION_COOKIE_NAME,
        _extract_csrf_token,
        _extract_session_cookie,
        _parse_login_form,
        mock_db_connection as auth_mock_db,
        shared_rate_limiter,
    )

    with shared_rate_limiter(FakeRateLimitStore()):
        with auth_mock_db():
            form = client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            login = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
            session_cookie = _extract_session_cookie(login)
            assert session_cookie
            dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
            assert dashboard.status_code == 200
            logout_csrf = _extract_csrf_token(dashboard.text)
            response = client.post(
                "/admin/logout",
                data={"csrf_token": logout_csrf},
                cookies={SESSION_COOKIE_NAME: session_cookie},
            )
    assert response.status_code == 303
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_admin_exception_handler_500_has_cache_headers(
    authenticated_admin: dict[str, str],
    preview_mode: None,
) -> None:
    with mock_db_connection():
        with patch(
            "app.admin_preview.build_preview_acquisition_dashboard_data",
            side_effect=HTTPException(status_code=500, detail="simulated failure"),
        ):
            response = client.get("/admin", cookies=authenticated_admin)
    assert response.status_code == 500
    _assert_admin_cache_headers(response)


@pytest.mark.integration
def test_static_assets_keep_intended_cache_policy() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    cache_values = _header_values(response.headers, "cache-control")
    assert cache_values == [] or "no-store" not in cache_values[0].lower()
    assert "content-security-policy" not in response.headers


@pytest.mark.integration
def test_public_home_has_no_admin_cache_policy() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "cache-control" not in response.headers


@pytest.mark.integration
def test_admin_cache_control_appears_once_on_login(preview_mode: None) -> None:
    response = client.get("/admin/login")
    names = [name.lower() for name, _ in response.headers.multi_items()]
    counts = Counter(names)
    assert counts["cache-control"] == 1


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(
        content=b"x",
        headers={"Cache-Control": "public, max-age=3600"},
    )
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == ADMIN_CACHE_CONTROL


@pytest.mark.integration
def test_middleware_replaces_weaker_route_cache_directive() -> None:
    async def _weak_cache_endpoint(request: Request) -> Response:
        return Response(
            content=b"probe",
            media_type="text/plain",
            headers={"Cache-Control": "no-cache"},
        )

    route = Route("/admin/__test_weak_cache__", _weak_cache_endpoint, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = client.get("/admin/__test_weak_cache__")
        assert response.status_code == 200
        _assert_admin_cache_headers(response)
    finally:
        app.router.routes.remove(route)
