"""Integration tests for admin Cache-Control: no-store, private (#337)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import admin_auth, admin_routes, audit_service, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_cache_policy import ADMIN_CACHE_CONTROL, apply_admin_cache_headers
from app.admin_routes import PREVIEW_SESSION_TOKEN
from app.main import app
from tests import test_admin_auth
from tests.test_admin_auth import (
    FakeRateLimitStore,
    mock_db_connection,
    shared_rate_limiter,
    _extract_csrf_token,
    _fetch_login_form,
    _login,
)

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

_session_store: dict[str, dict[str, Any]] = {}


def _cache_header_values(response: Any) -> list[str]:
    headers = response.headers
    if hasattr(headers, "get_list"):
        return headers.get_list("cache-control")
    if hasattr(headers, "getlist"):
        return headers.getlist("cache-control")
    value = headers.get("cache-control")
    return [value] if value else []


def _assert_single_no_store(response: Any) -> None:
    cache_values = _cache_header_values(response)
    assert cache_values == [ADMIN_CACHE_CONTROL], cache_values


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()
    _session_store.clear()
    client.cookies.clear()
    test_admin_auth.client.cookies.clear()


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, Any], None, None]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        row = _session_store.get(th)
        if row is None or row.get("revoked_at") is not None:
            return None
        return row

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in _session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    def _revoke_session(conn: Any, *, token_hash: str) -> bool:
        row = _session_store.get(token_hash)
        if row is None:
            return False
        row["revoked_at"] = datetime.now(timezone.utc)
        return True

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch.object(db, "revoke_admin_session", side_effect=_revoke_session),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_pipeline_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        db_conn.return_value.__exit__.return_value = None
        cookies = {SESSION_COOKIE_NAME: raw_token}
        with patch.object(admin_routes._crm, "list_contacts", return_value=[]):
            response = client.get("/admin/contacts", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.integration
def test_login_get_has_no_store() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_single_no_store(response)


@pytest.mark.integration
def test_anonymous_admin_redirect_has_no_store() -> None:
    response = client.get("/admin/briefs")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")
    _assert_single_no_store(response)


@pytest.mark.integration
def test_failed_login_post_has_no_store() -> None:
    with mock_db_connection():
        login_page = client.get("/admin/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        assert match is not None
        cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: login_page.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]}
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": match.group(1),
            },
            cookies=cookies,
        )
    assert response.status_code == 401
    _assert_single_no_store(response)


@pytest.mark.integration
def test_successful_login_redirect_has_no_store(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        response = _login()
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    _assert_single_no_store(response)


@pytest.mark.integration
def test_logout_redirect_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(admin_routes._crm, "list_contacts", return_value=[]):
        response = client.post(
            "/admin/logout",
            data={"csrf_token": authenticated_admin["csrf_token"]},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    _assert_single_no_store(response)


@pytest.mark.integration
def test_authenticated_html_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(admin_routes._crm, "list_contacts", return_value=[]):
        response = client.get("/admin/contacts", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    _assert_single_no_store(response)


@pytest.mark.integration
def test_authenticated_json_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    batch_id = uuid4()
    commit_result = {
        "batch": {"id": batch_id, "status": "completed", "checksum": "abc"},
        "idempotent": False,
        "summary_counts": {"created": 1},
    }
    with patch.object(admin_routes._crm, "commit_linkedin_import", return_value=commit_result):
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            json={"connections": []},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_404_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    response = client.get("/admin/does-not-exist", cookies=authenticated_admin["cookies"])
    assert response.status_code == 404
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_422_validation_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(audit_service, "list_events", return_value=([], 0)):
        response = client.get(
            "/admin/audit?page=not-an-integer",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 422
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_400_csrf_failure_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        "/admin/logout",
        data={"csrf_token": "invalid-token"},
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_429_login_throttle_has_no_store(rate_limit_store: FakeRateLimitStore) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            for _ in range(5):
                response = _login(password="wrong", csrf_token=csrf_token, cookies=cookies)
                assert response.status_code == 401
                csrf_token = _extract_csrf_token(response.text)
                flow_cookie = response.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow_cookie:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow_cookie
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
            )
    assert response.status_code == 429
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_500_exception_handler_has_no_store(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(
        audit_service,
        "list_events",
        side_effect=HTTPException(status_code=500, detail="simulated failure"),
    ):
        response = client.get("/admin/audit", cookies=authenticated_admin["cookies"])
    assert response.status_code == 500
    _assert_single_no_store(response)


@pytest.mark.integration
def test_admin_503_database_unavailable_has_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    response = client.get(
        "/admin/briefs/503",
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code == 503
    _assert_single_no_store(response)


@pytest.mark.integration
def test_public_home_unchanged() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers.get("cache-control") is None


@pytest.mark.integration
def test_static_asset_unchanged_with_admin_cookie(authenticated_admin: dict[str, Any]) -> None:
    response = client.get("/assets/admin.css", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert response.headers.get("cache-control") is None


@pytest.mark.integration
def test_middleware_overrides_weaker_downstream_cache_header() -> None:
    original = admin_routes.admin_login_form

    def _login_with_weak_cache(request: Any, next: str | None = None) -> Any:
        response = original(request, next)
        response.headers["Cache-Control"] = "max-age=3600, public"
        return response

    with mock_db_connection(), patch.object(
        admin_routes, "admin_login_form", side_effect=_login_with_weak_cache
    ):
        response = client.get("/admin/login")
    assert response.status_code == 200
    _assert_single_no_store(response)


@pytest.mark.integration
def test_preview_admin_html_has_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    response = client.get(
        "/admin/briefs",
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code == 200
    _assert_single_no_store(response)


@pytest.mark.unit
def test_apply_admin_cache_headers_on_json_response() -> None:
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "private, max-age=60"})
    apply_admin_cache_headers(response)
    assert _cache_header_values(response) == [ADMIN_CACHE_CONTROL]
