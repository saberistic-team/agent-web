"""Uvicorn integration tests for trusted admin login source resolution."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from tests.test_admin_auth import TEST_LIMITER_SECRET, TEST_HASH, TEST_PASSWORD, TEST_SECRET, TEST_USERNAME

REPO_ROOT = Path(__file__).resolve().parent.parent
TRUSTED_PROXY_CIDRS = "10.0.0.0/8,103.21.244.0/22"
RENDER_PROXY_IP = "10.0.0.1"
CLOUDFLARE_PROXY_IP = "103.21.244.50"
TRUSTED_CLIENT_IP = "198.51.100.55"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping uvicorn proxy integration test")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(base_url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {base_url}")


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.integration
def test_uvicorn_trusted_proxy_chain_throttles_rotated_spoofed_headers() -> None:
    database_url = _require_database_url()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **dict(os.environ),
        "BASE_URL": base_url,
        "DATABASE_URL": database_url,
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "ADMIN_LOGIN_RATE_LIMIT": "2",
        "ADMIN_TRUSTED_PROXY_CIDRS": TRUSTED_PROXY_CIDRS,
        "UVICORN_FORWARDED_ALLOW_IPS": "127.0.0.0/8",
    }
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
            "127.0.0.0/8",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_server(base_url)
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def login_attempt(leftmost_spoof: str) -> int:
            login_get = urllib.request.Request(f"{base_url}/admin/login")
            with opener.open(login_get, timeout=5) as response:
                html = response.read().decode("utf-8")
            csrf_token = _extract_csrf_token(html)
            chain = (
                f"{leftmost_spoof}, {TRUSTED_CLIENT_IP}, "
                f"{CLOUDFLARE_PROXY_IP}, {RENDER_PROXY_IP}"
            )
            data = (
                f"username=ghost&password={TEST_PASSWORD}&csrf_token={csrf_token}"
            ).encode("utf-8")
            request = urllib.request.Request(
                f"{base_url}/admin/login",
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Forwarded-For": chain,
                },
            )
            try:
                with opener.open(request, timeout=5) as response:
                    return int(response.status)
            except urllib.error.HTTPError as exc:
                return int(exc.code)

        statuses = [login_attempt(f"203.0.113.{index}") for index in range(3)]
        assert statuses[0] == 401
        assert statuses[1] == 401
        assert statuses[2] == 429
        assert any(cookie.name == LOGIN_FLOW_COOKIE_NAME for cookie in jar)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
