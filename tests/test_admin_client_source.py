"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    CLOUDFLARE_EDGE_CIDRS,
    ClientSourcePath,
    RENDER_PLATFORM_CIDRS,
    normalize_ip_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings

# Representative Cloudflare edge address from published ranges.
CF_EGRESS_IP = "173.245.48.10"
RENDER_PEER = "127.0.0.1"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "203.0.113.88"
UNTRUSTED_PEER = "198.51.100.10"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.fixture
def trusted_render_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,10.0.0.0/8,::1/128",
    )
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_PRESET", raising=False)


@pytest.mark.unit
def test_normalize_ipv4_mapped_ipv6() -> None:
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"


@pytest.mark.unit
def test_normalize_bracketed_ipv6_with_port() -> None:
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"


@pytest.mark.unit
def test_normalize_rejects_invalid() -> None:
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers=[_header("x-forwarded-for", "203.0.113.99")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == UNTRUSTED_PEER
    assert result.path is ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_direct_spoof_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers=[_header("x-forwarded-for", "203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == UNTRUSTED_PEER
    assert result.path is ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[
            _header(
                "x-forwarded-for",
                f"203.0.113.50, {REAL_CLIENT}, {CF_EGRESS_IP}",
            ),
            _header("cf-connecting-ip", REAL_CLIENT),
        ],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT
    assert result.path is ClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_xff_walk(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PEER}")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT
    assert result.path is ClientSourcePath.TRUSTED_XFF_WALK


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.5/32")
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, 10.0.0.5")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == UNTRUSTED_PEER
    assert result.path is ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers=[_header("cf-connecting-ip", "203.0.113.99")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == UNTRUSTED_PEER
    assert result.path is ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[
            _header("x-forwarded-for", f"203.0.113.1, {CF_EGRESS_IP}"),
            _header("cf-connecting-ip", REAL_CLIENT),
            _header("forwarded", 'for=203.0.113.99;proto=https'),
        ],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT
    assert result.path is ClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[_header("forwarded", f'for={REAL_CLIENT};proto=https;by={RENDER_PEER}')],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT
    assert result.path is ClientSourcePath.FORWARDED_HEADER


@pytest.mark.unit
def test_overlong_chain_falls_back_to_peer(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    hops = ", ".join(f"10.0.0.{i}" for i in range(1, 40))
    request = _request_with_client(
        RENDER_PEER,
        headers=[_header("x-forwarded-for", hops)],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == RENDER_PEER
    assert result.path is ClientSourcePath.OVERLONG_CHAIN


@pytest.mark.unit
def test_whitespace_and_empty_xff_elements(
    trusted_render_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[_header("x-forwarded-for", f"  {REAL_CLIENT}  , , {RENDER_PEER} ")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT


@pytest.mark.unit
def test_cloudflare_render_preset_expands_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_PRESET", "cloudflare-render")
    settings = get_settings()
    request = _request_with_client(
        RENDER_PEER,
        headers=[_header("x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PEER}")],
    )
    result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
    assert result.source == REAL_CLIENT


@pytest.mark.unit
def test_preset_includes_documented_platform_ranges() -> None:
    assert "127.0.0.1/32" in RENDER_PLATFORM_CIDRS
    assert "10.0.0.0/8" in RENDER_PLATFORM_CIDRS
    assert any("173.245.48.0" in cidr for cidr in CLOUDFLARE_EDGE_CIDRS)


@pytest.mark.unit
def test_telemetry_excludes_raw_ip_addresses(
    trusted_render_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers=[_header("x-forwarded-for", REAL_CLIENT)],
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    blob = caplog.text + str(caplog.records)
    assert REAL_CLIENT not in blob
    assert UNTRUSTED_PEER not in blob
    assert "x-forwarded-for" not in blob.lower()
    assert any(
        getattr(record, "client_source_path", None)
        == ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED.value
        for record in caplog.records
    )


@pytest.fixture
def admin_login_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("correct-horse-battery-staple"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_PRESET", raising=False)
    admin_auth.reset_login_rate_limiter()
    from tests.test_admin_auth import _login_flows, _session_store

    _login_flows.clear()
    _session_store.clear()


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    admin_login_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spoofed X-Forwarded-For values must not mint fresh source admissions."""
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        _login,
        mock_db_connection,
        shared_rate_limiter,
    )

    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with mock_db_connection():
            for i in range(5):
                response = _login(
                    username="ghost",
                    password="wrong",
                    headers={"X-Forwarded-For": f"203.0.113.{i}"},
                )
                if i < 2:
                    assert response.status_code == 401
                elif i == 2:
                    assert response.status_code == 429
                    break

    source_keys = {
        key
        for key in store.rows
        if key == admin_auth.build_source_rate_limit_key("testclient")
    }
    assert len(source_keys) == 1
    rows_blob = repr(store.rows)
    assert REAL_CLIENT not in rows_blob
    assert "x-forwarded-for" not in rows_blob.lower()


@pytest.mark.integration
def test_uvicorn_trusted_proxy_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn proxy-header trust with the deployment start flags."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("DATABASE_URL", "")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    cmd = [
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
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        settings = get_settings()
        scope = {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", f"203.0.113.50, 127.0.0.1".encode()),
            ],
            "client": ("127.0.0.1", 54321),
            "method": "GET",
            "path": "/health",
        }
        request = Request(scope)
        result = resolve_admin_login_client_source(request, settings, emit_telemetry=False)
        assert result.source == "203.0.113.50"
        assert result.path is ClientSourcePath.TRUSTED_XFF_WALK
    finally:
        proc.terminate()
        proc.wait(timeout=10)
