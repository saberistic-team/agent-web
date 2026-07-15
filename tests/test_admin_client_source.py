"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_address,
    parse_forwarded_header,
    parse_x_forwarded_for,
    reset_client_source_telemetry_counters,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app

RENDER_TRUSTED_PEER = "10.0.0.1"
RENDER_INTERNAL_HOP = "10.0.0.2"
CLOUDFLARE_EDGE = "173.245.48.1"
REAL_CLIENT = "198.51.100.10"
SPOOFED_CLIENT = "203.0.113.99"
DIRECT_PEER = "198.51.100.55"


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


def _settings_with_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128",
    )
    monkeypatch.setenv("ADMIN_TRUSTED_CLOUDFLARE_CIDRS", "173.245.48.0/20")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_counters()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(DIRECT_PEER, "direct_peer")


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(DIRECT_PEER, "direct_peer")


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_spoofed_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "xff_right_to_left")


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {RENDER_INTERNAL_HOP}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "xff_right_to_left")


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {RENDER_INTERNAL_HOP}".encode(),
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(DIRECT_PEER, "direct_peer")


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
            (b"x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(DIRECT_PEER, "direct_peer")


@pytest.mark.unit
def test_forwarded_header_precedence_over_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (b"forwarded", f'for="{REAL_CLIENT}";proto=https'.encode()),
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {CLOUDFLARE_EDGE}".encode()),
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "forwarded_rfc7239")


@pytest.mark.unit
def test_cf_connecting_ip_used_only_with_verified_cloudflare_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (b"forwarded", f'for="{CLOUDFLARE_EDGE}";proto=https'.encode()),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "cf_connecting_ip_verified")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_address(raw) == expected


@pytest.mark.unit
def test_xff_parsing_handles_whitespace_and_empty_elements() -> None:
    assert parse_x_forwarded_for(" 203.0.113.1 , , 10.0.0.5 ") == [
        "203.0.113.1",
        "10.0.0.5",
    ]


@pytest.mark.unit
def test_overlong_forward_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    chain = ", ".join(f"203.0.113.{index % 250}" for index in range(40))
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", chain.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution("unknown", "malformed_headers")


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.1")],
    )
    with caplog.at_level(logging.WARNING):
        resolve_admin_login_client_source(request, settings)
    assert "ignored forwarding headers from untrusted peer" in caplog.text
    assert DIRECT_PEER not in caplog.text
    assert "203.0.113.1" not in caplog.text


@pytest.mark.unit
def test_resolution_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode())],
    )
    with caplog.at_level(logging.INFO):
        admin_auth.client_ip(request, settings)
    assert "Admin login client source resolved" in caplog.text
    assert REAL_CLIENT not in caplog.text
    assert CLOUDFLARE_EDGE not in caplog.text


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_deployment_proxy_flags_start_and_serve_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot uvicorn with the same proxy-header flags declared in render.yaml."""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")

    port = _reserve_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                with httpx.Client() as http_client:
                    response = http_client.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert ready
        with httpx.Client() as http_client:
            health = http_client.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.unit
def test_parse_forwarded_header_extracts_ipv6_for_token() -> None:
    assert parse_forwarded_header('for="[2001:db8::9]";proto=https') == ["2001:db8::9"]
