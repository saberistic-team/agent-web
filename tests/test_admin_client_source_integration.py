"""ASGI integration tests for admin client-source trust through Uvicorn config (#239)."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _render_start_command(port: int) -> list[str]:
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = None
    for line in text.splitlines():
        if line.strip().startswith("startCommand:"):
            match = line.split(":", 1)[1].strip()
            break
    assert match is not None
    command = match.replace("$PORT", str(port))
    return command.split()


@pytest.mark.integration
def test_uvicorn_render_start_command_exposes_trust_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live Uvicorn using render.yaml startCommand serves /health trust metadata."""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$aaaaaaaaaaaaaaaaaaaaaa$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum!!")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "UVICORN_FORWARDED_ALLOW_IPS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1",
    )

    port = _free_port()
    cmd = _render_start_command(port)
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 25
        payload = None
        while time.time() < deadline:
            try:
                response = httpx.get(f"{base}/health", timeout=1.0)
                if response.status_code == 200:
                    payload = response.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        assert payload is not None, "uvicorn did not become ready"
        summary = payload.get("admin_client_source")
        assert summary is not None
        assert summary["trust_enabled"] is True
        assert summary["uvicorn_proxy_headers"] is False
        assert "10.0.0.0/8" in summary["uvicorn_forwarded_allow_ips"]
        assert "203.0.113" not in str(summary)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
