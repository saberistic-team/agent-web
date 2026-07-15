"""Integration tests for admin client source resolution behind Uvicorn."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_respects_trusted_proxy_boundary() -> None:
    port = _free_port()
    env = {
        **dict(__import__("os").environ),
        "ADMIN_TRUSTED_PROXY_CIDRS": "127.0.0.1",
        "ADMIN_TRUSTED_EDGE_CIDRS": "203.0.113.0/24",
        "DATABASE_URL": "",
        "BASE_URL": "http://127.0.0.1",
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
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1",
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{origin}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("uvicorn did not become ready")

        policy = response.json()["admin_client_source_policy"]
        assert policy["mode"] == "trusted_proxy_cidrs"
        assert policy["trusted_proxy_network_count"] == 1

        spoofed = httpx.get(
            f"{origin}/health",
            headers={
                "X-Forwarded-For": "203.0.113.99, 203.0.113.1",
            },
            timeout=5.0,
        )
        assert spoofed.status_code == 200
        assert spoofed.json()["status"] == "ok"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
