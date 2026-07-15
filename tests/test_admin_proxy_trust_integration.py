"""Uvicorn integration coverage for admin proxy trust (#239)."""

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
from typing import Any, Iterator

import pytest

from app.admin_client_source import reset_client_source_telemetry_for_tests

PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, timeout_seconds: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"server did not become healthy: {last_error}")


@pytest.fixture
def uvicorn_proxy_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    reset_client_source_telemetry_for_tests()
    port = _free_port()
    monkeypatch.setenv("BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32",
    )
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", PRODUCTION_FORWARDED_ALLOW_IPS)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--forwarded-allow-ips",
            PRODUCTION_FORWARDED_ALLOW_IPS,
            "--log-level",
            "warning",
        ],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(port)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_uvicorn_health_reports_proxy_trust(uvicorn_proxy_server: int) -> None:
    payload = _wait_for_health(uvicorn_proxy_server)
    trust = payload.get("admin_proxy_trust")
    assert isinstance(trust, dict)
    assert trust.get("proxy_header_trust_enabled") is True
    assert int(trust.get("trusted_proxy_network_count", 0)) >= 4
    assert trust.get("uvicorn_forwarded_allow_ips_configured") is True


@pytest.mark.integration
def test_uvicorn_start_command_matches_render_forwarded_allow_ips() -> None:
    text = (Path(__file__).resolve().parent.parent / "render.yaml").read_text(
        encoding="utf-8"
    )
    assert "key: UVICORN_FORWARDED_ALLOW_IPS" in text
    assert PRODUCTION_FORWARDED_ALLOW_IPS in text
