"""Tests for trusted admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    DEFAULT_RENDER_TRUSTED_PROXY_CIDRS,
    SourceResolutionPath,
    client_ip,
    normalize_ip_address,
    resolve_admin_login_client_source,
    uvicorn_forwarded_allow_ips_arg,
)
from app.config import get_settings

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "172.64.0.1"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
UNTRUSTED_PEER = "198.51.100.10"


def _request(
    *,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.fixture
def trusted_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    trusted_proxy_settings: None,
) -> None:
    settings = get_settings()
    for header in (
        "203.0.113.99",
        "203.0.113.99, 203.0.113.100",
    ):
        resolution = resolve_admin_login_client_source(
            _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": header}),
            settings,
        )
        assert resolution.source == UNTRUSTED_PEER
        assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    trusted_proxy_settings: None,
) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers={
                "X-Forwarded-For": f"203.0.113.1, {CLIENT_B}, {CLOUDFLARE_EDGE}",
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_B
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_settings: None) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers={"X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed(trusted_proxy_settings: None) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=UNTRUSTED_PEER,
            headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    trusted_proxy_settings: None,
) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=UNTRUSTED_PEER,
            headers={
                "CF-Connecting-IP": "203.0.113.1",
                "X-Forwarded-For": f"203.0.113.1, {CLOUDFLARE_EDGE}",
            },
        ),
        settings,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING


@pytest.mark.unit
def test_header_precedence_prefers_cf_connecting_ip_with_verified_edge(
    trusted_proxy_settings: None,
) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers={
                "CF-Connecting-IP": CLIENT_A,
                "X-Forwarded-For": f"{CLIENT_B}, {CLOUDFLARE_EDGE}",
                "Forwarded": f'for="{CLIENT_B}";proto=https',
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.CLOUDFLARE_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(trusted_proxy_settings: None) -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers={"Forwarded": f'for="{CLIENT_A}";proto=https'},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_overlong_forwarding_chain_fails_closed(trusted_proxy_settings: None) -> None:
    settings = get_settings()
    long_chain = ", ".join(f"10.0.0.{index}" for index in range(1, 40))
    resolution = resolve_admin_login_client_source(
        _request(peer=RENDER_PROXY, headers={"X-Forwarded-For": long_chain}),
        settings,
    )
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    resolution = resolve_admin_login_client_source(_request(peer=None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_uses_default_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            peer=RENDER_PROXY,
            headers={"X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A


@pytest.mark.unit
def test_uvicorn_forwarded_allow_ips_matches_defaults() -> None:
    assert uvicorn_forwarded_allow_ips_arg() == ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)


@pytest.mark.unit
def test_untrusted_forwarding_telemetry_is_sampled(
    trusted_proxy_settings: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.WARNING)
    for _ in range(101):
        resolve_admin_login_client_source(
            _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": "203.0.113.1"}),
            settings,
        )
    messages = [
        record.message
        for record in caplog.records
        if record.message.startswith("Admin login source resolution rejected")
    ]
    assert len(messages) == 1


@pytest.mark.unit
def test_client_ip_wrapper_matches_resolver(trusted_proxy_settings: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}"},
    )
    assert client_ip(request, settings) == CLIENT_A
