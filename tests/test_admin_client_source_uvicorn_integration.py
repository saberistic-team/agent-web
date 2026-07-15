"""Integration test exercising Uvicorn proxy-header trust with admin login limiter."""

from __future__ import annotations

import re
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.main import app
from tests.test_admin_auth import (
    RENDER_TRUSTED,
    FakeRateLimitStore,
    mock_db_connection,
    shared_rate_limiter,
)

TEST_PASSWORD = "wrong-password"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "integration-secret-32-characters-min"
TEST_USERNAME = "operator"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


class _SimulateRenderProxy:
    def __init__(self, inner: Any, proxy_ip: str = "10.0.0.1") -> None:
        self.inner = inner
        self.proxy_ip = proxy_ip

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.proxy_ip, 0)
        await self.inner(scope, receive, send)


def _proxy_stack(proxy_ip: str = "10.0.0.1") -> Any:
    return _SimulateRenderProxy(
        ProxyHeadersMiddleware(app, trusted_hosts=RENDER_TRUSTED),
        proxy_ip=proxy_ip,
    )


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.integration
def test_uvicorn_proxy_middleware_and_limiter_share_one_source_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    admin_env: None,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", RENDER_TRUSTED)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    admin_auth.reset_login_rate_limiter()

    proxy_client = TestClient(_proxy_stack(), follow_redirects=False)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            for index in range(3):
                form = proxy_client.get("/admin/login")
                assert form.status_code == 200
                csrf_token = _extract_csrf(form.text)
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": f"user-{index}",
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=form.cookies,
                    headers={
                        "X-Forwarded-For": f"203.0.113.{index}, 203.0.113.77, 10.0.0.1",
                    },
                )
                if index < 2:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 429

    assert len(rate_limit_store.rows) == 1
    assert admin_auth.build_source_rate_limit_key("203.0.113.77") in rate_limit_store.rows
