"""Integration tests for admin proxy trust through Uvicorn deployment settings."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app import admin_auth
from app.config import get_settings
from app.main import app

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not start on {host}:{port}")


@pytest.fixture
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", "104.16.0.0/13")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.0/8")
    monkeypatch.delenv("ADMIN_TRUST_CLOUDFLARE_EDGE", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.integration
def test_uvicorn_health_reports_proxy_trust_settings(
    proxy_trust_env: None,
) -> None:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.0/8",
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server("127.0.0.1", port)
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
        payload = response.json()
        trust = payload["admin_proxy_trust"]
        assert trust["proxy_headers_enabled"] is True
        assert trust["trusted_proxy_configured"] is True
        assert trust["trusted_edge_configured"] is True
        assert trust["resolution_model"] == "trusted_hop_walk"
        assert trust["forwarded_allow_ips"] == get_settings().uvicorn_forwarded_allow_ips
    finally:
        server.should_exit = True
        thread.join(timeout=5)
