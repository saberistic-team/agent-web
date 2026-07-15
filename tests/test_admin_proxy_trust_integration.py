"""Integration tests for admin login source trust via Uvicorn (#239)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import httpx
import pytest
from argon2 import PasswordHasher
from uvicorn.config import Config

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _uvicorn_server(*, trusted_cidrs: str, port: int) -> Generator[None, None, None]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_USERNAME": TEST_USERNAME,
            "ADMIN_PASSWORD_HASH": TEST_HASH,
            "ADMIN_SESSION_SECRET": TEST_SECRET,
            "BASE_URL": f"http://127.0.0.1:{port}",
            "ADMIN_PREVIEW_MODE": "1",
            "ADMIN_TRUSTED_PROXY_CIDRS": trusted_cidrs,
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
        "--forwarded-allow-ips",
        "",
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(command, env=env)
    try:
        deadline = time.time() + 20
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client:
            while time.time() < deadline:
                try:
                    response = client.get("/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                process.terminate()
                stdout, stderr = process.communicate(timeout=2)
                raise RuntimeError(
                    f"uvicorn did not become ready\nstdout={stdout!r}\nstderr={stderr!r}"
                )
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.integration
def test_uvicorn_config_matches_deployment_forwarded_allow_ips() -> None:
    config = Config("app.main:app", forwarded_allow_ips="")
    assert config.forwarded_allow_ips == ""
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips ''" in render_yaml


@pytest.mark.integration
def test_uvicorn_health_reports_source_trust_mode() -> None:
    port = _free_port()
    with _uvicorn_server(
        trusted_cidrs="127.0.0.0/8,127.0.0.1/32",
        port=port,
    ):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
            payload = client.get("/health").json()
    assert payload["admin_source_trust_mode"] == "trusted_proxy_boundary"
