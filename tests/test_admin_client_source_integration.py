"""Integration tests for admin login source resolution through Uvicorn."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Generator
from unittest.mock import patch

import httpx
import pytest
import uvicorn

from app import admin_auth
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.main import app
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    mock_db_connection,
    shared_rate_limiter,
)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _uvicorn_server(forwarded_allow_ips: str) -> Generator[str, None, None]:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        forwarded_allow_ips=forwarded_allow_ips,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    with patch("app.main.db.init_db"):
        thread.start()
        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("uvicorn failed to start")
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


def _login_post(
    base_url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=10.0) as http:
        form = http.get("/admin/login")
        csrf_token = _extract_csrf(form.text)
        cookies = dict(form.cookies)
        return http.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
            headers=headers or {},
        )


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


@pytest.mark.integration
def test_uvicorn_trusted_proxy_chain_rate_limits_stable_source(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_PROXY", "false")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")

    with (
        shared_rate_limiter(rate_limit_store),
        mock_db_connection(),
        _uvicorn_server("127.0.0.1") as base_url,
    ):
        stable_client = "198.51.100.55"
        for spoof in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
            headers = {
                "X-Forwarded-For": f"{spoof}, {stable_client}, 127.0.0.1",
            }
            response = _login_post(base_url, headers=headers)
            assert response.status_code == 401

        throttled = _login_post(
            base_url,
            headers={"X-Forwarded-For": f"203.0.113.99, {stable_client}, 127.0.0.1"},
        )
        assert throttled.status_code == 429

        source_key = admin_auth.build_source_rate_limit_key(stable_client)
        assert source_key in rate_limit_store.rows
        assert all(
            admin_auth.build_account_rate_limit_key(TEST_USERNAME) != key
            for key in rate_limit_store.rows
        )


@pytest.mark.integration
def test_uvicorn_direct_peer_ignores_spoofed_headers() -> None:
    with mock_db_connection(), _uvicorn_server("127.0.0.1") as base_url:
        response = _login_post(
            base_url,
            headers={
                "X-Forwarded-For": "203.0.113.99",
                "CF-Connecting-IP": "203.0.113.88",
                "Forwarded": 'for="203.0.113.77";proto=https',
            },
        )
        assert response.status_code == 401
        assert LOGIN_FLOW_COOKIE_NAME in response.cookies
