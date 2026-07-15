"""Integration tests for admin proxy trust + limiter behavior (#239)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import client_ip
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    _parse_login_form,
    mock_db_connection,
    shared_rate_limiter,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

RENDER_PROXY = "10.0.0.1"
REAL_CLIENT = "198.51.100.10"
SPOOFED_CLIENT = "203.0.113.99"

_proxy_app = ProxyHeadersMiddleware(
    app,
    trusted_hosts="10.0.0.0/8",
)
integration_client = TestClient(_proxy_app, follow_redirects=False)


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_admin_auth import TEST_HASH, TEST_SECRET

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    admin_auth.reset_login_rate_limiter()


def _login_with_proxy(
    *,
    headers: dict[str, str],
) -> Any:
    with mock_db_connection():
        csrf_token, cookies = _parse_login_form(integration_client.get("/admin/login"))
        return integration_client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers,
        )


@pytest.mark.integration
def test_uvicorn_proxy_middleware_stack_uses_trusted_hop_resolver() -> None:
    """Exercise the same ProxyHeadersMiddleware used in deployment."""
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode()),
            (b"host", b"testserver"),
        ],
        "client": (RENDER_PROXY, 50000),
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    assert client_ip(request, settings) == REAL_CLIENT


@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_source_bucket(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with patch("app.admin_client_source._immediate_peer", return_value=RENDER_PROXY):
            for index in range(3):
                response = _login_with_proxy(
                    headers={"X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}"},
                )
                assert response.status_code == 401

            blocked = _login_with_proxy(
                headers={"X-Forwarded-For": f"203.0.113.250, {REAL_CLIENT}"},
            )
            assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert source_key in rate_limit_store.rows
    assert len(rate_limit_store.rows) <= 2


@pytest.mark.integration
def test_untrusted_direct_peer_ignores_forwarded_headers(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        for index in range(3):
            csrf_token, cookies = _parse_login_form(integration_client.get("/admin/login"))
            with mock_db_connection():
                response = integration_client.post(
                    "/admin/login",
                    data={
                        "username": f"user-{index}",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                )
            assert response.status_code in {400, 401}

        csrf_token, cookies = _parse_login_form(integration_client.get("/admin/login"))
        with mock_db_connection():
            blocked = integration_client.post(
                "/admin/login",
                data={
                    "username": "user-final",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.250"},
            )
    assert blocked.status_code == 429
    source_key = admin_auth.build_source_rate_limit_key("testclient")
    assert source_key in rate_limit_store.rows


@pytest.mark.integration
def test_limiter_rows_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with patch("app.admin_client_source._immediate_peer", return_value=RENDER_PROXY):
            _login_with_proxy(
                headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"},
            )

    for row in rate_limit_store.rows.values():
        assert REAL_CLIENT not in str(row)
        assert SPOOFED_CLIENT not in str(row)
