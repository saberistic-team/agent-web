"""Integration tests for admin security headers on the response matrix (#308)."""

from __future__ import annotations

import re
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
from app.admin_response_policy import (
    parse_csp_directives,
    validate_admin_csp,
)
from app.main import app

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
    _session_store.clear()


def _header_values(headers: Any, name: str) -> list[str]:
    raw = headers.get_list(name) if hasattr(headers, "get_list") else [headers.get(name)]
    return [value for value in raw if value is not None]


def _assert_admin_security_headers(response: Any) -> str:
    """Assert required admin headers and return the CSP policy string."""
    for header_name in (
        "content-security-policy",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
        "x-frame-options",
        "x-xss-protection",
    ):
        values = _header_values(response.headers, header_name)
        assert len(values) == 1, f"{header_name} must appear once, got {values}"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-xss-protection"] == "0"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "microphone=()" in response.headers["permissions-policy"]
    assert "geolocation=()" in response.headers["permissions-policy"]
    assert "strict-transport-security" not in response.headers
    policy = response.headers["content-security-policy"]
    validate_admin_csp(policy)
    return policy


def _csp_nonce_from_policy(policy: str) -> str:
    match = re.search(r"'nonce-([^']+)'", policy)
    assert match, f"nonce missing from policy: {policy}"
    return match.group(1)


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
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")


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
def test_admin_html_pages_emit_security_headers(
    preview_mode: None,
    path: str,
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_redirect_to_login_has_security_headers() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    policy = _assert_admin_security_headers(response)
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.integration
def test_admin_login_required_exception_redirect_has_headers() -> None:
    response = client.get("/admin/briefs")
    assert response.status_code == 303
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_json_commit_has_security_headers(authenticated_admin: dict[str, str]) -> None:
    with mock_db_connection():
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            cookies=authenticated_admin,
            json={"batch_label": "test", "rows": []},
        )
    assert response.status_code in {400, 422, 503}
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_fastapi_validation_error_has_security_headers() -> None:
    response = client.post("/admin/login", data={})
    assert response.status_code == 422
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_validation_failure_has_security_headers() -> None:
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
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_404_shell_has_security_headers(preview_mode: None) -> None:
    response = client.get("/admin/no-such-section")
    assert response.status_code == 404
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_503_preview_fixture_has_security_headers(preview_mode: None) -> None:
    response = client.get("/admin/briefs/503")
    assert response.status_code == 503
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_admin_pipeline_json_error_has_security_headers(
    preview_mode: None,
) -> None:
    response = client.post(
        "/admin/pipeline/not-a-uuid/stage",
        data={"stage": "qualifying", "csrf_token": "bad"},
    )
    assert response.status_code in {400, 404, 422}
    _assert_admin_security_headers(response)


@pytest.mark.integration
def test_csp_nonce_unique_per_response(preview_mode: None) -> None:
    first = client.get("/admin/login")
    second = client.get("/admin/login")
    nonce_a = _csp_nonce_from_policy(first.headers["content-security-policy"])
    nonce_b = _csp_nonce_from_policy(second.headers["content-security-policy"])
    assert nonce_a != nonce_b


@pytest.mark.integration
def test_imports_inline_script_nonce_matches_csp(
    authenticated_admin: dict[str, str],
) -> None:
    with mock_db_connection():
        response = client.get("/admin/imports", cookies=authenticated_admin)
    assert response.status_code == 200
    policy = _assert_admin_security_headers(response)
    nonce = _csp_nonce_from_policy(policy)
    assert f'nonce="{nonce}"' in response.text
    assert response.text.count(f'nonce="{nonce}"') == 1


@pytest.mark.integration
def test_header_values_are_not_request_injection_vectors(
    preview_mode: None,
) -> None:
    evil = "<script>alert(1)</script>"
    response = client.get(
        f"/admin/login?next={evil}",
        headers={"User-Agent": evil, "X-Correlation-Id": evil},
    )
    assert response.status_code == 200
    for value in response.headers.values():
        assert "<script>" not in value.lower()
        assert evil not in value


@pytest.mark.integration
def test_static_assets_have_nosniff_without_admin_csp() -> None:
    response = client.get("/assets/admin.css")
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" not in response.headers
    assert "text/css" in response.headers.get("content-type", "")


@pytest.mark.integration
def test_hsts_present_only_for_https_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "https://staging.example.test")
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert response.headers.get("strict-transport-security") == "max-age=31536000"


@pytest.mark.integration
def test_public_home_has_no_admin_csp() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


@pytest.mark.integration
def test_csp_directives_are_explicit_not_wildcard(preview_mode: None) -> None:
    response = client.get("/admin")
    policy = response.headers["content-security-policy"]
    parsed = parse_csp_directives(policy)
    for directive, value in parsed.items():
        assert "*" not in value, directive
        assert "unsafe-eval" not in value
        assert "unsafe-inline" not in value


@pytest.mark.integration
def test_admin_rate_limit_429_has_security_headers() -> None:
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
    _assert_admin_security_headers(blocked)


@pytest.mark.integration
def test_admin_headers_appear_once_on_login(preview_mode: None) -> None:
    response = client.get("/admin/login")
    names = [name.lower() for name, _ in response.headers.multi_items()]
    counts = Counter(names)
    for header in (
        "content-security-policy",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
        "x-frame-options",
    ):
        assert counts[header] == 1
