"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
from typing import Any

from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_ip_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app

RENDER_PROXY = "10.0.0.2"
CLOUDFLARE_EDGE = "172.64.0.1"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "198.51.100.50"
UNTRUSTED_PEER = "203.0.113.10"


def _request(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32",
    )
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("  2001:db8::1  ") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.55") == "203.0.113.55"
    assert normalize_ip_address("[2001:db8::1]:8443") == "2001:db8::1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("203.0.113.1:notaport") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)

    single = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    multi = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    assert resolve_admin_login_client_source(single, settings) == ClientSourceResolution(
        UNTRUSTED_PEER,
        SourceResolutionPath.DIRECT_PEER,
    )
    assert resolve_admin_login_client_source(multi, settings).source == UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (
                b"x-forwarded-for",
                f"203.0.113.1, 203.0.113.2, {REAL_CLIENT}".encode(),
            ),
            (b"cf-ray", b"abc123-IAD"),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        REAL_CLIENT,
        SourceResolutionPath.CLOUDFLARE_CONNECTING_IP,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}".encode(),
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        REAL_CLIENT,
        SourceResolutionPath.TRUSTED_XFF_CHAIN,
    )


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {RENDER_PROXY}".encode(),
            ),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
            (b"cf-ray", b"edge-ray"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        UNTRUSTED_PEER,
        SourceResolutionPath.DIRECT_PEER,
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.99"),
            (b"x-forwarded-for", b"203.0.113.99"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path != SourceResolutionPath.CLOUDFLARE_CONNECTING_IP
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", b"203.0.113.1, 203.0.113.2"),
            (b"forwarded", b'for=203.0.113.3;proto=https'),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
            (b"cf-ray", b"edge-ray"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == SourceResolutionPath.CLOUDFLARE_CONNECTING_IP
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_xff_precedence_over_forwarded_without_cloudflare_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}".encode()),
            (b"forwarded", b'for=203.0.113.3;proto=https'),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("203.0.113.1, , 203.0.113.2", SourceResolutionPath.INVALID_FORWARDING),
        ("not-an-ip", SourceResolutionPath.INVALID_FORWARDING),
        ("", SourceResolutionPath.TRUSTED_PEER_NO_FORWARDING),
    ],
)
def test_malformed_and_empty_forwarding_cases(
    monkeypatch: pytest.MonkeyPatch,
    header_value: str,
    expected: SourceResolutionPath,
) -> None:
    settings = _trusted_settings(monkeypatch)
    headers: list[tuple[bytes, bytes]] = []
    if header_value:
        headers.append((b"x-forwarded-for", header_value.encode()))
    request = _request(peer=RENDER_PROXY, headers=headers)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == expected


@pytest.mark.unit
def test_overlong_forwarding_chain_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    request = _request(
        peer=RENDER_PROXY,
        headers=[(b"x-forwarded-for", chain.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_ipv6_and_mapped_ipv6_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (
                b"x-forwarded-for",
                b"2001:db8::5, ::ffff:203.0.113.9, 10.0.0.2",
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "2001:db8::5"
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_PROXY}".encode()),
            (b"cf-ray", b"edge-ray"),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
        ],
    )
    with caplog.at_level(logging.INFO):
        resolution = admin_auth.resolve_admin_login_client_source_with_telemetry(
            request,
            settings,
        )
    assert resolution.source == REAL_CLIENT
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.source_resolution_path == SourceResolutionPath.CLOUDFLARE_CONNECTING_IP.value
    message = caplog.text.lower()
    assert REAL_CLIENT not in caplog.text
    assert "x-forwarded-for" not in message
    assert "cf-connecting-ip" not in message


@pytest.mark.unit
def test_rotating_spoofed_headers_keep_stable_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    real_attacker = "203.0.113.55"
    first = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers=[
                (b"x-forwarded-for", f"203.0.113.1, 203.0.113.2, {real_attacker}".encode()),
                (b"cf-ray", b"edge-ray"),
                (b"cf-connecting-ip", real_attacker.encode()),
            ],
        ),
        settings,
    )
    second = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers=[
                (b"x-forwarded-for", f"203.0.113.4, 203.0.113.5, {real_attacker}".encode()),
                (b"cf-ray", b"edge-ray"),
                (b"cf-connecting-ip", real_attacker.encode()),
            ],
        ),
        settings,
    )
    assert first.source == real_attacker
    assert second.source == real_attacker
    assert admin_auth.build_source_rate_limit_key(first.source) == admin_auth.build_source_rate_limit_key(
        second.source
    )


@pytest.mark.unit
def test_sampled_invalid_forwarding_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    monkeypatch.setattr(
        "app.admin_client_source.random.random",
        lambda: 0.0,
    )
    request = _request(
        peer=RENDER_PROXY,
        headers=[(b"x-forwarded-for", b"definitely-not-valid")],
    )
    with caplog.at_level(logging.WARNING):
        resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING
    assert any(
        record.source_resolution_path == SourceResolutionPath.INVALID_FORWARDING.value
        for record in caplog.records
    )
    assert "definitely-not-valid" not in caplog.text


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.integration
def test_uvicorn_proxy_configuration_matches_deployment_limiter_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same Uvicorn forwarded-allow-ips setting used in render.yaml."""
    from argon2 import PasswordHasher
    from test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("wrong-password"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    admin_auth.reset_login_rate_limiter()

    port = _reserve_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # Production uses 127.0.0.1 only; Render peers (10.x) are not rewritten.
        # Use a non-loopback allowlist in tests so local httpx peers are not rewritten.
        forwarded_allow_ips="10.0.0.1",
    )
    server = uvicorn.Server(config)

    async def _wait_until_ready() -> None:
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200:
                    return
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
        raise RuntimeError("uvicorn server did not become ready")

    async def _exercise_limiter() -> None:
        async with httpx.AsyncClient() as http_client:
            for spoofed in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
                login_page = await http_client.get(f"http://127.0.0.1:{port}/admin/login")
                assert login_page.status_code == 200
                csrf_match = re.search(
                    r'name="csrf_token" value="([^"]+)"',
                    login_page.text,
                )
                assert csrf_match is not None
                flow_cookie = login_page.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                assert flow_cookie
                response = await http_client.post(
                    f"http://127.0.0.1:{port}/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong-password",
                        "csrf_token": csrf_match.group(1),
                    },
                    cookies={admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookie},
                    headers={
                        "X-Forwarded-For": spoofed,
                        "CF-Connecting-IP": spoofed,
                    },
                )
                assert response.status_code == 401

            login_page = await http_client.get(f"http://127.0.0.1:{port}/admin/login")
            csrf_match = re.search(
                r'name="csrf_token" value="([^"]+)"',
                login_page.text,
            )
            flow_cookie = login_page.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
            blocked = await http_client.post(
                f"http://127.0.0.1:{port}/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong-password",
                    "csrf_token": csrf_match.group(1) if csrf_match else "",
                },
                cookies={admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookie or ""},
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            assert blocked.status_code == 429

    with (
        patch("app.main.db.init_db"),
        shared_rate_limiter(store),
        mock_db_connection(),
    ):
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            asyncio.run(_wait_until_ready())
            asyncio.run(_exercise_limiter())
        finally:
            server.should_exit = True
            thread.join(timeout=5)
