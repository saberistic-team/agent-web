"""Integration tests for admin proxy trust via uvicorn (#239)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, *, timeout_seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"health never became ready: {last_error}")


@pytest.mark.integration
def test_uvicorn_health_reports_proxy_trust_configuration() -> None:
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,127.0.0.1",
            "ADMIN_CLOUDFLARE_EDGE_CIDRS": "198.51.100.0/24",
            "UVICORN_FORWARDED_ALLOW_IPS": "127.0.0.1",
        }
    )
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
        "--forwarded-allow-ips=127.0.0.1",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        payload = _wait_for_health(port)
        assert payload["status"] == "ok"
        assert payload["admin_proxy_trust"] == "configured"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.integration
def test_uvicorn_starts_with_render_proxy_flags_and_unconfigured_db() -> None:
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
            "UVICORN_FORWARDED_ALLOW_IPS": "127.0.0.1",
        }
    )
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
        "--forwarded-allow-ips=127.0.0.1",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        payload = _wait_for_health(port)
        assert payload["status"] == "ok"
        assert payload["admin_proxy_trust"] == "configured"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
