"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    DEFAULT_TRUSTED_PROXY_CIDRS,
    normalize_client_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

TRUSTED_PROXY_CIDRS = ",".join(DEFAULT_TRUSTED_PROXY_CIDRS)
CLOUDFLARE_TEST_CIDR = "198.41.128.0/17"
CLOUDFLARE_TEST_IP = "198.41.128.7"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", CLOUDFLARE_TEST_CIDR)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "untrusted_forwarded_rejected"

    request_multi = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    resolution_multi = resolve_admin_login_client_source(
        request_multi, settings, emit_telemetry=False
    )
    assert resolution_multi.source == "198.51.100.10"
    assert resolution_multi.path == "untrusted_forwarded_rejected"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": f"203.0.113.99, 198.51.100.55, {CLOUDFLARE_TEST_IP}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "198.51.100.55"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "127.0.0.1",
        headers={"X-Forwarded-For": f"203.0.113.50, {CLOUDFLARE_TEST_IP}, 127.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "xff_trusted_chain"
    assert resolution.trusted_peer is True


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.20",
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "198.51.100.20"
    assert resolution.path == "untrusted_forwarded_rejected"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.30",
        headers={
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.88",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "198.51.100.30"
    assert resolution.path == "untrusted_forwarded_rejected"


@pytest.mark.unit
def test_header_precedence_xff_over_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.2",
        headers={
            "X-Forwarded-For": f"203.0.113.60, {CLOUDFLARE_TEST_IP}",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "203.0.113.60"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_xff_only_cloudflare_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "127.0.0.1",
        headers={
            "X-Forwarded-For": CLOUDFLARE_TEST_IP,
            "CF-Connecting-IP": "203.0.113.61",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "203.0.113.61"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "127.0.0.1",
        headers={"Forwarded": 'for=203.0.113.70;proto=https, for=127.0.0.1;proto=https'},
    )
    resolution = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert resolution.source == "203.0.113.70"
    assert resolution.path == "forwarded_header"


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    malformed = _request_with_client(
        "127.0.0.1",
        headers={"X-Forwarded-For": "not-an-ip, 127.0.0.1"},
    )
    malformed_resolution = resolve_admin_login_client_source(
        malformed, settings, emit_telemetry=False
    )
    assert malformed_resolution.path == "malformed_forwarding"
    assert malformed_resolution.source == "127.0.0.1"

    overlong = _request_with_client(
        "127.0.0.1",
        headers={"X-Forwarded-For": ", ".join(f"203.0.113.{index}" for index in range(40))},
    )
    overlong_resolution = resolve_admin_login_client_source(
        overlong, settings, emit_telemetry=False
    )
    assert overlong_resolution.path == "malformed_forwarding"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_source_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request_with_client(
                    "198.51.100.40",
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                ),
                settings,
                emit_telemetry=False,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_telemetry_and_logs_contain_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    resolve_admin_login_client_source(
        _request_with_client(
            "198.51.100.50",
            headers={"X-Forwarded-For": "203.0.113.99"},
        ),
        settings,
    )
    combined = caplog.text
    assert "203.0.113.99" not in combined
    assert "198.51.100.50" not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert any(
        record.__dict__.get("source_resolution_path") == "untrusted_forwarded_rejected"
        for record in caplog.records
    )


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    repo_root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(repo_root, "render.yaml"), encoding="utf-8") as handle:
        render_yaml = handle.read()
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_CLOUDFLARE_EDGE_CIDRS" in render_yaml
    assert "--proxy-headers" in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert "127.0.0.1" in render_yaml
    assert "10.0.0.0/8" in render_yaml


@pytest.mark.unit
def test_health_reports_client_source_trust_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    client = TestClient(app)
    assert client.get("/health").json()["admin_client_source_trust"] == "direct_only"

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    assert client.get("/health").json()["admin_client_source_trust"] == "configured"


def _wait_for_http_ok(url: str, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_configuration_resists_header_spoofing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn --proxy-headers with the same flags as render.yaml."""
    port = _find_free_port()
    env = {
        **os.environ,
        "DATABASE_URL": "",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "BASE_URL": f"http://127.0.0.1:{port}",
        "ADMIN_TRUSTED_PROXY_CIDRS": TRUSTED_PROXY_CIDRS,
        "ADMIN_CLOUDFLARE_EDGE_CIDRS": CLOUDFLARE_TEST_CIDR,
        "ADMIN_LOGIN_RATE_LIMIT": "3",
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
            "--proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1,10.0.0.0/8",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ok(f"{base}/health")
        health = httpx.get(f"{base}/health", timeout=5.0).json()
        assert health["admin_client_source_trust"] == "configured"

        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
        monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", CLOUDFLARE_TEST_CIDR)
        resolution = resolve_admin_login_client_source(
            _request_with_client(
                "127.0.0.1",
                headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.55"},
            ),
            get_settings(),
            emit_telemetry=False,
        )
        assert resolution.source == "198.51.100.55"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.unit
def test_client_ip_wrapper_matches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.9",
        headers={"X-Forwarded-For": "203.0.113.80, 10.0.0.9"},
    )
    assert admin_auth.client_ip(request, settings) == "203.0.113.80"
