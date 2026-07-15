"""Uvicorn integration coverage for admin client-source proxy trust."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Generator
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
UVICORN_PORT = 18991
UVICORN_BASE_URL = f"http://127.0.0.1:{UVICORN_PORT}"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture
def uvicorn_admin_server(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> Generator[str, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", UVICORN_BASE_URL)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    admin_auth.reset_login_rate_limiter()

    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=UVICORN_PORT,
        log_level="error",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    with patch("app.db.init_db"):
        thread.start()

        deadline = time.monotonic() + 15
        with httpx.Client(base_url=UVICORN_BASE_URL, timeout=2.0) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").json().get("client_source_trust") == "verified-proxy-hop-v1":
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:
                server.should_exit = True
                thread.join(timeout=2)
                raise RuntimeError("uvicorn server failed to start")

        with shared_rate_limiter(rate_limit_store), mock_db_connection():
            try:
                yield UVICORN_BASE_URL
            finally:
                server.should_exit = True
                thread.join(timeout=5)


def _extract_login_form(html: str) -> str | None:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else None


def _login_attempt(
    client: httpx.Client,
    *,
    forwarded_for: str,
) -> int:
    form = client.get("/admin/login")
    csrf_token = _extract_login_form(form.text)
    flow_cookie = form.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    assert csrf_token
    assert flow_cookie
    response = client.post(
        "/admin/login",
        data={
            "username": "ghost",
            "password": "wrong",
            "csrf_token": csrf_token,
        },
        cookies={LOGIN_FLOW_COOKIE_NAME: flow_cookie},
        headers={"X-Forwarded-For": forwarded_for},
    )
    return response.status_code


@pytest.mark.integration
def test_uvicorn_trusted_peer_rotating_spoofed_xff_shares_one_source_bucket(
    uvicorn_admin_server: str,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with httpx.Client(base_url=uvicorn_admin_server, timeout=5.0) as client:
        shared_client = "198.51.100.20"
        assert _login_attempt(client, forwarded_for=f"203.0.113.1, {shared_client}") == 401
        assert _login_attempt(client, forwarded_for=f"203.0.113.2, {shared_client}") == 401
        assert _login_attempt(client, forwarded_for=f"203.0.113.3, {shared_client}") == 429

    source_key = admin_auth.build_source_rate_limit_key(shared_client)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.integration
def test_uvicorn_health_reports_proxy_trust_model(uvicorn_admin_server: str) -> None:
    with httpx.Client(base_url=uvicorn_admin_server, timeout=5.0) as client:
        payload: dict[str, Any] = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["client_source_trust"] == "verified-proxy-hop-v1"
