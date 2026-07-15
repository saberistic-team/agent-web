"""Integration tests for admin login proxy trust (#239)."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    DEFAULT_RENDER_TRUSTED_PROXY_CIDRS,
    uvicorn_forwarded_allow_ips_arg,
)
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_USERNAME,
    FakeRateLimitStore,
    _extract_csrf_token,
    _mock_claim_admin_login_flow,
    _mock_cleanup_stale_admin_login_flows,
    _mock_create_admin_login_flow,
    _mock_create_admin_session,
    _mock_get_admin_session_by_token_hash,
    _mock_revoke_admin_session,
    _mock_update_admin_session_csrf,
    mock_db_connection,
    shared_rate_limiter,
)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def proxy_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    admin_auth.reset_login_rate_limiter()

RENDER_PROXY = ("10.0.0.1", 50000)
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.51"
CLOUDFLARE_EDGE = "172.64.0.1"


def _proxy_wrapped_app():
    return ProxyHeadersMiddleware(
        app,
        trusted_hosts=uvicorn_forwarded_allow_ips_arg(),
    )


@pytest.fixture
def proxy_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS),
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    admin_auth.reset_login_rate_limiter()
    return TestClient(
        _proxy_wrapped_app(),
        follow_redirects=False,
        client=RENDER_PROXY,
    )


@contextmanager
def _route_db() -> Generator[MagicMock, None, None]:
    with patch("app.admin_routes.db.db_connection") as db_conn:
        with patch(
            "app.admin_routes.db.create_admin_login_flow",
            _mock_create_admin_login_flow,
        ):
            with patch(
                "app.admin_routes.db.cleanup_stale_admin_login_flows",
                _mock_cleanup_stale_admin_login_flows,
            ):
                with patch(
                    "app.admin_routes.db.claim_admin_login_flow",
                    _mock_claim_admin_login_flow,
                ):
                    with patch(
                        "app.admin_routes.db.create_admin_session",
                        _mock_create_admin_session,
                    ):
                        with patch(
                            "app.admin_routes.db.get_admin_session_by_token_hash",
                            _mock_get_admin_session_by_token_hash,
                        ):
                            with patch(
                                "app.admin_routes.db.update_admin_session_csrf",
                                _mock_update_admin_session_csrf,
                            ):
                                with patch(
                                    "app.admin_routes.db.revoke_admin_session",
                                    _mock_revoke_admin_session,
                                ):
                                    conn = MagicMock()
                                    db_conn.return_value.__enter__.return_value = conn
                                    db_conn.return_value.__exit__.return_value = None
                                    yield conn


def _login_form(proxy_client: TestClient) -> tuple[str, dict[str, str]]:
    with _route_db():
        response = proxy_client.get("/admin/login")
    csrf = _extract_csrf_token(response.text)
    cookies: dict[str, str] = {}
    flow = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    if flow:
        cookies[LOGIN_FLOW_COOKIE_NAME] = flow
    return csrf, cookies


def _login(
    proxy_client: TestClient,
    *,
    csrf_token: str,
    cookies: dict[str, str],
    headers: dict[str, str] | None = None,
) -> Any:
    with _route_db():
        return proxy_client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers or {},
        )


@pytest.mark.integration
def test_uvicorn_proxy_chain_rotating_spoofed_headers_share_one_bucket(
    proxy_client: TestClient,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        csrf, cookies = _login_form(proxy_client)
        trusted_chain = {
            "X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}",
        }
        assert _login(proxy_client, csrf_token=csrf, cookies=cookies, headers=trusted_chain).status_code == 401

        csrf, cookies = _login_form(proxy_client)
        assert _login(proxy_client, csrf_token=csrf, cookies=cookies, headers=trusted_chain).status_code == 401

        csrf, cookies = _login_form(proxy_client)
        rotated = {
            "X-Forwarded-For": f"203.0.113.99, {CLIENT_A}, {CLOUDFLARE_EDGE}",
        }
        blocked = _login(proxy_client, csrf_token=csrf, cookies=cookies, headers=rotated)
        assert blocked.status_code == 429
        assert admin_auth.LOGIN_THROTTLED_MESSAGE in blocked.text

        source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
        account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME)
        assert source_key in rate_limit_store.rows
        assert len(rate_limit_store.rows) == 2
        assert account_key in rate_limit_store.rows


@pytest.mark.integration
def test_uvicorn_proxy_chain_distinguishes_real_clients(
    proxy_client: TestClient,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        for client_ip in (CLIENT_A, CLIENT_B):
            csrf, cookies = _login_form(proxy_client)
            headers = {"X-Forwarded-For": f"{client_ip}, {CLOUDFLARE_EDGE}"}
            assert _login(proxy_client, csrf_token=csrf, cookies=cookies, headers=headers).status_code == 401

        assert admin_auth.build_source_rate_limit_key(CLIENT_A) in rate_limit_store.rows
        assert admin_auth.build_source_rate_limit_key(CLIENT_B) in rate_limit_store.rows


@pytest.mark.integration
def test_health_reports_proxy_trust_when_configured(
    proxy_client: TestClient,
) -> None:
    with _route_db():
        response = proxy_client.get("/health")
    payload = response.json()
    assert payload["admin_proxy_trust"]["configured"] is True
    assert payload["admin_proxy_trust"]["forwarded_allow_ips"] == uvicorn_forwarded_allow_ips_arg(
        get_settings()
    )


@pytest.mark.integration
def test_limiter_rows_and_logs_contain_no_raw_forwarding_data(
    proxy_client: TestClient,
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    with shared_rate_limiter(rate_limit_store):
        csrf, cookies = _login_form(proxy_client)
        headers = {
            "X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}",
            "CF-Connecting-IP": CLIENT_A,
        }
        _login(proxy_client, csrf_token=csrf, cookies=cookies, headers=headers)

    for row in rate_limit_store.rows.values():
        assert CLIENT_A not in str(row)
        assert CLOUDFLARE_EDGE not in str(row)

    assert not any(
        ip in record.message
        for record in caplog.records
        for ip in (CLIENT_A, CLOUDFLARE_EDGE)
    )
    assert any(
        getattr(record, "source_resolution_path", None)
        for record in caplog.records
    )
