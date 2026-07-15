"""Deployment and ASGI integration tests for admin proxy trust (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import resolve_admin_login_client_source
from app.config import get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PROXY = "10.0.0.5"
CLIENT_IP = "203.0.113.77"
ATTACKER_IP = "198.51.100.10"


def _render_yaml_text() -> str:
    return (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")


@pytest.mark.unit
def test_render_yaml_declares_proxy_trust_settings() -> None:
    text = _render_yaml_text()
    assert "--forwarded-allow-ips" in text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in text
    assert "10.0.0.0/8" in text
    assert "ADMIN_TRUST_PROXY_HEADERS" in text
    assert "--proxy-headers" not in text


@pytest.mark.unit
def test_render_start_command_matches_documentation() -> None:
    text = _render_yaml_text()
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "--forwarded-allow-ips" in docs
    assert text.count("--forwarded-allow-ips") == 1


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        TEST_HASH,
        TEST_SECRET,
        TEST_USERNAME,
        _parse_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    store = FakeRateLimitStore()
    proxy_client = TestClient(app, client=(RENDER_PROXY, 50000))
    with shared_rate_limiter(store):
        for _index in range(5):
            headers = {"X-Forwarded-For": f"{ATTACKER_IP}, {CLIENT_IP}, {RENDER_PROXY}"}
            with mock_db_connection():
                form = proxy_client.get("/admin/login")
                csrf_token, cookies = _parse_login_form(form)
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers=headers,
                )
            assert response.status_code == 401

        with mock_db_connection():
            form = proxy_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            blocked = proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"203.0.113.99, {CLIENT_IP}, {RENDER_PROXY}"},
            )
        assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_IP)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_does_not_bypass_app_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Uvicorn left-most client rewrite must not become the limiter key."""
    import asyncio

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$test$test",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1/32,10.0.0.0/8")
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "10.0.0.0/8"])
    settings = get_settings()
    chain = f"{ATTACKER_IP}, {CLIENT_IP}, {RENDER_PROXY}"
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [(b"x-forwarded-for", chain.encode("ascii"))],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        _ = message

    asyncio.run(wrapped(scope, receive, send))

    request = Request(scope)
    resolution = resolve_admin_login_client_source(request, settings)
    assert request.client is not None
    assert resolution.source == "unknown"
    assert resolution.source != ATTACKER_IP
