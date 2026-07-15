"""Integration tests for admin login source resolution through Uvicorn."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "198.41.128.10"
REAL_CLIENT = "203.0.113.77"
SPOOFED_LEFTMOST = "203.0.113.99"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def uvicorn_proxy_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run the app with the same Uvicorn flags declared in render.yaml."""
    port = _free_port()
    env = {
        **dict(__import__("os").environ),
        "DATABASE_URL": "",
        "ADMIN_USERNAME": "operator",
        "ADMIN_PASSWORD_HASH": "x",
        "ADMIN_SESSION_SECRET": "test-session-secret-32chars-minimum",
        "BASE_URL": f"http://127.0.0.1:{port}",
        "ADMIN_TRUST_PROXY_HEADERS": "true",
        "ADMIN_TRUSTED_PROXY_CIDRS": "127.0.0.1,10.0.0.0/8,198.41.128.0/17",
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-proxy-headers",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    with httpx.Client(base_url=base_url, timeout=2.0) as client:
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            process.terminate()
            process.wait(timeout=5)
            pytest.fail("uvicorn server did not become ready")
        yield base_url
    process.terminate()
    process.wait(timeout=10)


@pytest.mark.integration
def test_uvicorn_integration_trusted_chain_uses_real_client(
    uvicorn_proxy_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1,10.0.0.0/8,198.41.128.0/17")
    settings = get_settings()

    scope = {
        "type": "http",
        "headers": [
            (
                b"x-forwarded-for",
                f"{SPOOFED_LEFTMOST}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode(),
            )
        ],
        "client": (RENDER_LB, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    source = admin_auth.client_ip(request, settings)
    assert source == REAL_CLIENT

    key_a = admin_auth.build_source_rate_limit_key(source)
    scope["headers"] = [
        (
            b"x-forwarded-for",
            f"203.0.113.1, {REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode(),
        )
    ]
    key_b = admin_auth.build_source_rate_limit_key(admin_auth.client_ip(Request(scope), settings))
    assert key_a == key_b

    with httpx.Client(base_url=uvicorn_proxy_server, timeout=5.0) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json().get("status") == "ok"


@pytest.mark.integration
def test_uvicorn_start_command_disables_proxy_header_rewrite() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--no-proxy-headers" in render_yaml
