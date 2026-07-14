"""Tests for admin authentication, sessions, CSRF, and route protection."""

from __future__ import annotations

import re
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

_login_flows: dict[str, dict[str, Any]] = {}
_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _mock_create_admin_login_flow(conn: MagicMock, **kwargs: Any) -> int:
    flow_hash = kwargs["flow_token_hash"]
    _login_flows[flow_hash] = {
        "id": len(_login_flows) + 1,
        "flow_token_hash": flow_hash,
        "csrf_token_hash": kwargs["csrf_token_hash"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": kwargs["expires_at"],
        "consumed_at": None,
    }
    return int(_login_flows[flow_hash]["id"])


def _mock_get_admin_login_flow_by_token_hash(
    conn: MagicMock,
    flow_token_hash: str,
) -> dict[str, Any] | None:
    return _login_flows.get(flow_token_hash)


def _mock_consume_admin_login_flow(
    conn: MagicMock,
    *,
    flow_token_hash: str,
) -> None:
    row = _login_flows.get(flow_token_hash)
    if row is not None:
        row["consumed_at"] = datetime.now(timezone.utc)


def _mock_create_admin_session(conn: MagicMock, **kwargs: Any) -> int:
    session_id = len(_session_store) + 1
    _session_store[kwargs["token_hash"]] = {
        "id": session_id,
        "token_hash": kwargs["token_hash"],
        "admin_username": kwargs["admin_username"],
        "created_at": datetime.now(timezone.utc),
        "expires_at": kwargs["expires_at"],
        "revoked_at": None,
        "csrf_token_hash": kwargs.get("csrf_token_hash"),
    }
    return session_id


def _mock_get_admin_session_by_token_hash(
    conn: MagicMock,
    token_hash: str,
) -> dict[str, Any] | None:
    return _session_store.get(token_hash)


def _mock_update_admin_session_csrf(
    conn: MagicMock,
    *,
    session_id: int,
    csrf_token_hash: str,
) -> None:
    for row in _session_store.values():
        if row["id"] == session_id:
            row["csrf_token_hash"] = csrf_token_hash


def _mock_revoke_admin_session(conn: MagicMock, *, token_hash: str) -> None:
    row = _session_store.get(token_hash)
    if row is not None:
        row["revoked_at"] = datetime.now(timezone.utc)


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with ExitStack() as stack:
        db_conn_patch = stack.enter_context(patch("app.admin_routes.db.db_connection"))
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_login_flow", _mock_create_admin_login_flow)
        )
        stack.enter_context(
            patch(
                "app.admin_routes.db.get_admin_login_flow_by_token_hash",
                _mock_get_admin_login_flow_by_token_hash,
            )
        )
        stack.enter_context(
            patch("app.admin_routes.db.consume_admin_login_flow", _mock_consume_admin_login_flow)
        )
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_session", _mock_create_admin_session)
        )
        stack.enter_context(
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                _mock_get_admin_session_by_token_hash,
            )
        )
        stack.enter_context(
            patch("app.admin_routes.db.update_admin_session_csrf", _mock_update_admin_session_csrf)
        )
        stack.enter_context(
            patch("app.admin_routes.db.revoke_admin_session", _mock_revoke_admin_session)
        )
        db_conn_patch.return_value.__enter__.return_value = conn
        db_conn_patch.return_value.__exit__.return_value = None
        yield conn


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _parse_login_form(response: Any) -> tuple[str, dict[str, str]]:
    assert response.status_code == 200
    csrf_token = _extract_csrf_token(response.text)
    cookies: dict[str, str] = {}
    flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    if flow_cookie:
        cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
    return csrf_token, cookies


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    with mock_db_connection():
        response = client.get("/admin/login")
    return _parse_login_form(response)


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str | None = None,
    cookies: dict[str, str] | None = None,
    next_path: str | None = None,
) -> Any:
    with mock_db_connection():
        if csrf_token is None:
            if cookies is None:
                csrf_token, cookies = _parse_login_form(client.get("/admin/login"))
            else:
                raise ValueError("csrf_token is required when cookies are provided")
        data = {
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        }
        if next_path is not None:
            data["next"] = next_path
        return client.post("/admin/login", data=data, cookies=cookies or {})


def _extract_session_cookie(response: Any) -> str | None:
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    return cookie


def _session_row(
    *,
    token_hash: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    csrf_token_hash: str | None = None,
    session_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        "revoked_at": revoked_at,
        "csrf_token_hash": csrf_token_hash,
    }


@pytest.mark.unit
def test_admin_auth_settings_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    settings = get_settings()
    assert not settings.admin_auth_configured


@pytest.mark.unit
def test_verify_admin_credentials_accepts_valid_pair() -> None:
    settings = get_settings()
    assert admin_auth.verify_admin_credentials(TEST_USERNAME, TEST_PASSWORD, settings)


@pytest.mark.unit
def test_verify_admin_credentials_rejects_wrong_password() -> None:
    settings = get_settings()
    assert not admin_auth.verify_admin_credentials(TEST_USERNAME, "wrong-password", settings)


@pytest.mark.unit
def test_verify_admin_credentials_rejects_wrong_username() -> None:
    settings = get_settings()
    assert not admin_auth.verify_admin_credentials("unknown-user", TEST_PASSWORD, settings)


@pytest.mark.unit
def test_csrf_value_round_trip() -> None:
    raw = admin_auth.generate_csrf_value()
    stored = admin_auth.hash_csrf_token(raw)
    assert admin_auth.verify_csrf_value(raw, stored)


@pytest.mark.unit
def test_csrf_value_rejects_tampered_token() -> None:
    raw = admin_auth.generate_csrf_value()
    stored = admin_auth.hash_csrf_token(raw)
    tampered = raw[:-1] + ("a" if raw[-1] != "a" else "b")
    assert not admin_auth.verify_csrf_value(tampered, stored)


@pytest.mark.unit
def test_csrf_value_rejects_missing_or_malformed() -> None:
    stored = admin_auth.hash_csrf_token(admin_auth.generate_csrf_value())
    assert not admin_auth.verify_csrf_value("", stored)
    assert not admin_auth.verify_csrf_value("bad-token", None)
    assert not admin_auth.verify_csrf_value("bad-token", "")


@pytest.mark.unit
@pytest.mark.integration
def test_login_logout_flow() -> None:
    with mock_db_connection():
        login = _login()
        assert login.status_code == 303
        assert login.headers["location"] == "/admin"
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        token_hash = admin_auth.hash_session_token(session_cookie)
        assert token_hash in _session_store

        dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        assert dashboard.status_code == 200
        assert TEST_USERNAME in dashboard.text
        logout_csrf = _extract_csrf_token(dashboard.text)

        logout = client.post(
            "/admin/logout",
            data={"csrf_token": logout_csrf},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
        assert logout.status_code == 303
        assert logout.headers["location"] == "/admin/login"
        assert _session_store[token_hash]["revoked_at"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_admin_dashboard_redirects_to_login() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login?next=")


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_nested_admin_route_redirects_to_login() -> None:
    response = client.get("/admin/reports")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_login_invalid_credentials_use_generic_message() -> None:
    response = _login(password="not-the-password")
    assert response.status_code == 401
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_unknown_username_uses_same_error_message() -> None:
    bad_password = _login(username="ghost", password="nope")
    bad_username = _login(username="ghost", password=TEST_PASSWORD)
    assert bad_password.status_code == 401
    assert bad_username.status_code == 401
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in bad_password.text
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in bad_username.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_invalid_csrf_token() -> None:
    csrf_token, cookies = _fetch_login_form()
    with mock_db_connection():
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": "not-a-valid-token",
            },
            cookies=cookies,
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_cross_session_csrf_token() -> None:
    csrf_a, _cookies_a = _fetch_login_form()
    _csrf_b, cookies_b = _fetch_login_form()
    with mock_db_connection():
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf_a,
            },
            cookies=cookies_b,
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_missing_login_flow_cookie() -> None:
    csrf_token, _cookies = _fetch_login_form()
    client.cookies.clear()
    with mock_db_connection():
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf_token,
            },
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rejects_expired_login_flow() -> None:
    with mock_db_connection():
        csrf_token, cookies = _fetch_login_form()
        flow_cookie = cookies[LOGIN_FLOW_COOKIE_NAME]
        flow_hash = admin_auth.hash_session_token(flow_cookie)
        _login_flows[flow_hash]["expires_at"] = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        response = client.post(
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
@pytest.mark.integration
def test_login_rejects_replayed_login_flow() -> None:
    with mock_db_connection():
        csrf_token, cookies = _fetch_login_form()
        first = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
        assert first.status_code == 401

        replay = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )
    assert replay.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in replay.text


@pytest.mark.unit
@pytest.mark.integration
def test_login_rate_limiting() -> None:
    for _ in range(5):
        response = _login(password="wrong")
        assert response.status_code == 401

    blocked = _login(password="wrong")
    assert blocked.status_code == 429
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text


@pytest.mark.unit
@pytest.mark.integration
def test_expired_session_redirects_to_login() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    with mock_db_connection():
        response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_revoked_session_redirects_to_login() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection():
        response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_login_regenerates_session_and_revokes_prior_cookie() -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)
    _session_store[old_hash] = _session_row(token_hash=old_hash, session_id=1)

    with mock_db_connection():
        csrf_token, flow_cookies = _parse_login_form(client.get("/admin/login"))
        flow_cookies[SESSION_COOKIE_NAME] = old_token
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf_token,
            },
            cookies=flow_cookies,
        )
        assert response.status_code == 303
        assert _session_store[old_hash]["revoked_at"] is not None
        new_cookie = _extract_session_cookie(response)
        assert new_cookie
        assert new_cookie != old_token


@pytest.mark.unit
@pytest.mark.integration
def test_session_cookie_flags() -> None:
    with mock_db_connection():
        response = _login()
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.unit
@pytest.mark.integration
def test_login_flow_cookie_flags() -> None:
    with mock_db_connection():
        response = client.get("/admin/login")
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.unit
@pytest.mark.integration
def test_login_honors_safe_next_redirect() -> None:
    with mock_db_connection():
        response = _login(next_path="/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
@pytest.mark.integration
def test_login_ignores_external_next_redirect() -> None:
    with mock_db_connection():
        response = _login(next_path="https://evil.example/phish")
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
@pytest.mark.integration
def test_logout_rejects_invalid_session_csrf() -> None:
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        response = client.post(
            "/admin/logout",
            data={"csrf_token": "not-the-right-token"},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 400
    assert admin_auth.INVALID_REQUEST_MESSAGE in response.json()["detail"]


@pytest.mark.unit
@pytest.mark.integration
def test_logout_rejects_cross_session_csrf_token() -> None:
    with mock_db_connection():
        login = _login()
        session_cookie = _extract_session_cookie(login)
        assert session_cookie

        dashboard_a = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        csrf_a = _extract_csrf_token(dashboard_a.text)

        dashboard_b = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
        _extract_csrf_token(dashboard_b.text)

        response = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_a},
            cookies={SESSION_COOKIE_NAME: session_cookie},
        )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_revoked_session_logout_csrf_fails_closed() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(admin_auth.generate_csrf_value())
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        csrf_token_hash=csrf_hash,
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection():
        response = client.post(
            "/admin/logout",
            data={"csrf_token": admin_auth.generate_csrf_value()},
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.unit
@pytest.mark.integration
def test_no_registration_or_reset_routes_exist() -> None:
    with mock_db_connection():
        for path in (
            "/admin/register",
            "/admin/signup",
            "/admin/password-reset",
            "/admin/forgot-password",
        ):
            response = client.get(path)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_admin_unconfigured_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    response = client.get("/admin/login")
    assert response.status_code == 503
