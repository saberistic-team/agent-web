"""Tests for verified-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_source,
    parse_trusted_proxy_networks,
    reset_untrusted_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app

TRUSTED_PROXIES = "10.0.0.0/8,127.0.0.1,::1"
RENDER_LB = "10.0.0.5"
REAL_CLIENT = "198.51.100.10"
SPOOFED = "203.0.113.99"


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


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", TRUSTED_PROXIES)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.mark.unit
def test_normalize_client_source_formats() -> None:
    assert normalize_client_source("203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_source("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("2001:db8::1") == "2001:db8::1"
    assert normalize_client_source("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_source("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_source("") is None
    assert normalize_client_source("not-an-ip") is None


@pytest.mark.unit
def test_parse_trusted_proxy_networks() -> None:
    nets = parse_trusted_proxy_networks("10.0.0.0/8,203.0.113.1,2001:db8::1/128")
    assert len(nets) == 3


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(trusted_proxy_env: None) -> None:
    settings = get_settings()
    for header_value in (
        SPOOFED,
        f"{SPOOFED}, {REAL_CLIENT}",
    ):
        request = _request_with_client(
            REAL_CLIENT,
            headers=[(b"x-forwarded-for", header_value.encode())],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == REAL_CLIENT
        assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_leftmost_spoof(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers=[(b"x-forwarded-for", f"{SPOOFED}, {REAL_CLIENT}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_xff"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers=[(b"x-forwarded-for", REAL_CLIENT.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "forwarded_xff")


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.50",
        headers=[(b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_LB}".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == "untrusted_peer"
    assert resolution.source == "198.51.100.50"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        REAL_CLIENT,
        headers=[(b"cf-connecting-ip", SPOOFED.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_header_precedence_forwarded_over_xff_and_cf(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers=[
            (
                b"forwarded",
                f'for=203.0.113.1;proto=https, for="{REAL_CLIENT}"'.encode(),
            ),
            (b"x-forwarded-for", SPOOFED.encode()),
            (b"cf-connecting-ip", SPOOFED.encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_rfc7239"


@pytest.mark.unit
def test_malformed_and_overlong_xff_falls_back(trusted_proxy_env: None) -> None:
    settings = get_settings()
    overlong = ",".join(["203.0.113.1"] * 40)
    request = _request_with_client(
        RENDER_LB,
        headers=[(b"x-forwarded-for", overlong.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == RENDER_LB


@pytest.mark.unit
def test_empty_xff_elements_and_whitespace(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers=[(b"x-forwarded-for", f"  , {REAL_CLIENT} , ".encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_no_trusted_proxies_uses_direct_peer() -> None:
    settings = get_settings()
    request = _request_with_client(
        REAL_CLIENT,
        headers=[(b"x-forwarded-for", SPOOFED.encode())],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(REAL_CLIENT, "direct_peer")


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_telemetry(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    reset_untrusted_forwarding_telemetry()
    request = _request_with_client(
        REAL_CLIENT,
        headers=[(b"x-forwarded-for", SPOOFED.encode())],
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
        resolve_admin_login_client_source(request, settings)
    assert any(
        "rejected forwarding headers" in record.message for record in caplog.records
    )


@pytest.mark.unit
def test_admission_logs_resolution_path_not_raw_ip(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin_auth.reset_login_rate_limiter()
    request = _request_with_client(
        RENDER_LB,
        headers=[(b"x-forwarded-for", REAL_CLIENT.encode())],
    )
    settings = get_settings()
    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.side_effect = Exception("offline")
        with caplog.at_level(logging.WARNING):
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")
    joined = caplog.text + str(caplog.records)
    assert REAL_CLIENT not in joined
    assert RENDER_LB not in joined
    assert "203.0.113" not in joined


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_single_source_bucket(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating left-most XFF values must not create fresh limiter rows."""
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")

    async def app_with_proxy(scope: dict[str, Any], receive: Any, send: Any) -> None:
        mutable = dict(scope)
        mutable["client"] = (RENDER_LB, 12345)
        await app(mutable, receive, send)

    # Use resolver-level limiter keys to prove bucket stability without full route stack.
    settings = get_settings()
    keys: set[str] = set()
    for index in range(5):
        request = _request_with_client(
            RENDER_LB,
            headers=[(b"x-forwarded-for", f"{SPOOFED}.{index}, {REAL_CLIENT}".encode())],
        )
        source = admin_auth.client_ip(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert len(keys) == 1


@pytest.mark.unit
def test_render_yaml_proxy_settings_consistent() -> None:
    from pathlib import Path

    render = Path("render.yaml").read_text()
    assert "--forwarded-allow-ips" in render
    assert "ADMIN_TRUSTED_PROXY_IPS" in render
    start = render.split("startCommand:", 1)[1].split("\n", 1)[0]
    env_block = render.split("ADMIN_TRUSTED_PROXY_IPS", 1)[1]
    env_value = env_block.split('value: "', 1)[1].split('"', 1)[0]
    for cidr in env_value.split(","):
        assert cidr.strip() in start


@pytest.mark.unit
@pytest.mark.integration
def test_asgi_login_limiter_with_trusted_proxy_peer(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise limiter admission with a Render-like trusted ASGI peer."""
    import asyncio

    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = get_settings()

    async def app_with_render_peer(scope: dict[str, Any], receive: Any, send: Any) -> None:
        mutable = dict(scope)
        mutable["client"] = (RENDER_LB, 12345)
        await app(mutable, receive, send)

    async def exercise() -> None:
        transport = ASGITransport(app=app_with_render_peer)
        async with httpx.AsyncClient(transport=transport, base_url="http://test"):
            request = _request_with_client(
                RENDER_LB,
                headers=[(b"x-forwarded-for", REAL_CLIENT.encode())],
            )
            for _ in range(2):
                admission = admin_auth.try_admit_login_attempt(
                    request, settings, username="ghost"
                )
                assert admission.admitted
            blocked = admin_auth.try_admit_login_attempt(
                request, settings, username="ghost"
            )
            assert blocked.throttled

    with shared_rate_limiter(store):
        asyncio.run(exercise())

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert source_key in store.rows
    assert len(store.rows) == 1

@pytest.mark.unit
@pytest.mark.integration
def test_limiter_rows_contain_no_raw_addresses(
    trusted_proxy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating spoofed XFF must not mint multiple limiter rows with raw IPs."""
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    settings = get_settings()

    with shared_rate_limiter(store):
        for index in range(5):
            request = _request_with_client(
                RENDER_LB,
                headers=[(b"x-forwarded-for", f"{SPOOFED}.{index}, {REAL_CLIENT}".encode())],
            )
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    assert len(store.rows) == 1
    serialized = str(store.rows)
    assert REAL_CLIENT not in serialized
    assert SPOOFED not in serialized
    for row in store.rows.values():
        assert "failure_count" in row
        assert "203.0.113" not in str(row)

