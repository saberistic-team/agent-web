"""Integration tests for admin proxy trust via Uvicorn ASGI serving."""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import contextmanager
from typing import Generator

import httpx
import pytest
import uvicorn
from argon2 import PasswordHasher

from app.admin_auth import build_source_rate_limit_key
from app.main import app
from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

CLIENT_IP = "203.0.113.55"
CLOUDFLARE_EDGE = "203.0.113.200"
TEST_TRUSTED_PROXY_IPS = "127.0.0.1,::1,10.0.0.0/8"
TEST_CLOUDFLARE_CIDRS = "203.0.113.200/32"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@contextmanager
def _uvicorn_server() -> Generator[str, None, None]:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{origin}/health", timeout=0.5)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("uvicorn server did not become ready")

    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def _httpx_login(
    origin: str,
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    with httpx.Client(base_url=origin, follow_redirects=False) as http:
        form = http.get("/admin/login")
        csrf_token = _extract_csrf_token(form.text)
        return http.post(
            "/admin/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": csrf_token,
            },
            headers=headers or {},
        )


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setattr("app.db.init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", TEST_TRUSTED_PROXY_IPS)
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_HEADERS", "true")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", TEST_CLOUDFLARE_CIDRS)


@pytest.mark.integration
def test_uvicorn_rotating_spoofed_leftmost_headers_share_one_source_bucket(
    proxy_env: None,
) -> None:
    store = FakeRateLimitStore()
    with shared_rate_limiter(store), mock_db_connection():
        with _uvicorn_server() as origin:
            for index in range(3):
                response = _httpx_login(
                    origin,
                    username="ghost",
                    password="wrong",
                    headers={
                        "X-Forwarded-For": (
                            f"203.0.113.{index}, {CLIENT_IP}, {CLOUDFLARE_EDGE}"
                        ),
                    },
                )
                assert response.status_code == 401

            blocked = _httpx_login(
                origin,
                username="ghost",
                password="wrong",
                headers={
                    "X-Forwarded-For": f"203.0.113.99, {CLIENT_IP}, {CLOUDFLARE_EDGE}",
                },
            )
            assert blocked.status_code == 429

    source_key = build_source_rate_limit_key(CLIENT_IP)
    assert len(store.rows) == 1
    assert source_key in store.rows
