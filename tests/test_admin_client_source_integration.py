"""Integration tests for admin client source through Uvicorn proxy middleware."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import ClientSourceResolutionPath, resolve_admin_login_client_source
from app.config import Settings

TRUSTED_PROXY = "10.0.0.1"
TRUSTED_SETTINGS = Settings(
    database_url="postgresql://test:test@localhost:5432/test",
    stripe_secret_key="",
    stripe_webhook_secret="",
    stripe_publishable_key="",
    resend_api_key="",
    from_email="noreply@example.com",
    notify_email="ops@example.com",
    base_url="http://testserver",
    plausible_domain="",
    plausible_api_key="",
    analytics_environment="test",
    admin_username="operator",
    admin_password_hash="hash",
    admin_session_secret="secret-secret-secret-secret",
    admin_trusted_proxy_cidrs=("10.0.0.0/8",),
)

PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,100.64.0.0/10,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


async def _scope_after_proxy_middleware(
    *,
    peer_host: str,
    headers: list[tuple[bytes, bytes]],
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def capture_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["scope"] = dict(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    proxy_app = ProxyHeadersMiddleware(capture_app, trusted_hosts=PRODUCTION_FORWARDED_ALLOW_IPS)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": headers,
        "client": (peer_host, 54321),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, Any]) -> None:
        return None

    await proxy_app(scope, receive, send)
    assert "scope" in captured
    return captured["scope"]


@pytest.mark.integration
def test_uvicorn_proxy_middleware_with_trusted_peer_and_spoofed_xff() -> None:
    scope = asyncio.run(
        _scope_after_proxy_middleware(
            peer_host=TRUSTED_PROXY,
            headers=[(b"x-forwarded-for", b"203.0.113.99, 198.51.100.10")],
        )
    )
    request = Request(scope)
    result = resolve_admin_login_client_source(request, TRUSTED_SETTINGS)
    assert result.source == "198.51.100.10"
    assert scope["client"][0] == "198.51.100.10"


@pytest.mark.integration
def test_uvicorn_proxy_middleware_ignores_xff_from_untrusted_peer() -> None:
    scope = asyncio.run(
        _scope_after_proxy_middleware(
            peer_host="203.0.113.77",
            headers=[(b"x-forwarded-for", b"203.0.113.99")],
        )
    )
    request = Request(scope)
    result = resolve_admin_login_client_source(request, TRUSTED_SETTINGS)
    assert result.source == "203.0.113.77"
    assert scope["client"][0] == "203.0.113.77"


@pytest.mark.integration
def test_limiter_key_stable_when_rotating_spoofed_headers_from_untrusted_peer() -> None:
    keys: set[str] = set()
    for index in range(5):
        scope = {
            "type": "http",
            "headers": [(b"x-forwarded-for", f"203.0.113.{index}".encode("ascii"))],
            "client": ("198.51.100.10", 12345),
            "method": "POST",
            "path": "/admin/login",
        }
        request = Request(scope)
        source = resolve_admin_login_client_source(request, TRUSTED_SETTINGS).source
        assert source == "198.51.100.10"
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert len(keys) == 1


@pytest.mark.integration
def test_health_route_reachable_through_proxy_middleware() -> None:
    from app.main import app

    proxy_app = ProxyHeadersMiddleware(app, trusted_hosts=PRODUCTION_FORWARDED_ALLOW_IPS)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=proxy_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/health")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.integration
def test_trusted_peer_cf_connecting_ip_path() -> None:
    scope = {
        "type": "http",
        "headers": [
            (b"cf-connecting-ip", b"203.0.113.50"),
            (b"x-forwarded-for", b"203.0.113.99, 198.51.100.10"),
        ],
        "client": (TRUSTED_PROXY, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    result = resolve_admin_login_client_source(request, TRUSTED_SETTINGS)
    assert result.source == "203.0.113.50"
    assert result.path is ClientSourceResolutionPath.CF_CONNECTING_IP
