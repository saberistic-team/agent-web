"""Tests for admin authentication, sessions, CSRF, and route protection."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


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


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _csrf_token() -> str:
    return admin_auth.generate_csrf_token(get_settings())


def _session_row(
    *,
    token_hash: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        "revoked_at": revoked_at,
    }


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str | None = None,
    next_path: str | None = None,
) -> Any:
    data = {
        "username": username,
        "password": password,
        "csrf_token": csrf_token or _csrf_token(),
    }
    if next_path is not None:
        data["next"] = next_path
    return client.post("/admin/login", data=data)


def _extract_session_cookie(response: Any) -> str | None:
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    return cookie


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
def test_csrf_token_round_trip() -> None:
    settings = get_settings()
    token = admin_auth.generate_csrf_token(settings)
    assert admin_auth.verify_csrf_token(token, settings)


@pytest.mark.unit
def test_csrf_token_rejects_tampered_signature() -> None:
    settings = get_settings()
    token = admin_auth.generate_csrf_token(settings)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert not admin_auth.verify_csrf_token(tampered, settings)


@pytest.mark.unit
@pytest.mark.integration
def test_login_logout_flow() -> None:
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.create_admin_session", return_value=1) as create_session:
            with patch("app.admin_routes.db.revoke_admin_session") as revoke_session:
                login = _login()
                assert login.status_code == 303
                assert login.headers["location"] == "/admin"
                session_cookie = _extract_session_cookie(login)
                assert session_cookie

                create_session.assert_called_once()
                token_hash = create_session.call_args.kwargs["token_hash"]
                row = _session_row(token_hash=token_hash)

                with patch(
                    "app.admin_routes.db.get_admin_session_by_token_hash",
                    return_value=row,
                ):
                    dashboard = client.get("/admin", cookies={SESSION_COOKIE_NAME: session_cookie})
                    assert dashboard.status_code == 200
                    assert 'class="admin-app"' in dashboard.text

                logout = client.post("/admin/logout", cookies={SESSION_COOKIE_NAME: session_cookie})
                assert logout.status_code == 303
                assert logout.headers["location"] == "/admin/login"
                revoke_session.assert_called_once()


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
    response = _login(csrf_token="9999999999.bad.bad")
    assert response.status_code == 400
    assert admin_auth.INVALID_CREDENTIALS_MESSAGE in response.text


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
    expired = _session_row(
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=expired,
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_revoked_session_redirects_to_login() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    revoked = _session_row(
        token_hash=token_hash,
        revoked_at=datetime.now(timezone.utc),
    )
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=revoked,
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_login_regenerates_session_and_revokes_prior_cookie() -> None:
    old_token = admin_auth.generate_session_token()
    old_hash = admin_auth.hash_session_token(old_token)

    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.create_admin_session", return_value=2) as create_session:
            with patch("app.admin_routes.db.revoke_admin_session") as revoke_session:
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": _csrf_token(),
                    },
                    cookies={SESSION_COOKIE_NAME: old_token},
                )
                assert response.status_code == 303
                revoke_session.assert_called_once_with(conn, token_hash=old_hash)
                new_cookie = _extract_session_cookie(response)
                assert new_cookie
                assert new_cookie != old_token
                assert create_session.call_count == 1


@pytest.mark.unit
@pytest.mark.integration
def test_session_cookie_flags() -> None:
    with mock_db_connection():
        with patch("app.admin_routes.db.create_admin_session", return_value=1):
            response = _login()
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.unit
@pytest.mark.integration
def test_login_honors_safe_next_redirect() -> None:
    with mock_db_connection():
        with patch("app.admin_routes.db.create_admin_session", return_value=1):
            response = _login(next_path="/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
@pytest.mark.integration
def test_login_ignores_external_next_redirect() -> None:
    with mock_db_connection():
        with patch("app.admin_routes.db.create_admin_session", return_value=1):
            response = _login(next_path="https://evil.example/phish")
    assert response.headers["location"] == "/admin"


@pytest.mark.unit
@pytest.mark.integration
def test_no_registration_or_reset_routes_exist() -> None:
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=None,
        ):
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
