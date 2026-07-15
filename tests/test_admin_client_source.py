"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app import admin_auth
from app.admin_client_source import (
    normalize_ip_address,
    reset_untrusted_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.proxy_trust_config import parse_cidr_list

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "173.245.55.100"
CLIENT_A = "203.0.113.50"
CLIENT_B = "198.51.100.10"
SPOOFED = "203.0.113.99"
DIRECT_PEER = "198.51.100.10"


def _settings(
    *,
    trust_proxy: bool = False,
    trusted_cidrs: str = "",
    cloudflare_cidrs: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        **{
            **base.__dict__,
            "admin_trust_proxy_headers": trust_proxy,
            "admin_trusted_proxy_cidrs": parse_cidr_list(trusted_cidrs),
            "admin_cloudflare_edge_cidrs": parse_cidr_list(cloudflare_cidrs),
        }
    )


def _request(
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_untrusted_forwarding_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.7") == "203.0.113.7"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    for header in (
        SPOOFED,
        f"{SPOOFED}, {CLIENT_B}",
        f"{SPOOFED}, {CLIENT_B}, {RENDER_PROXY}",
    ):
        resolution = resolve_admin_login_client_source(
            _request(DIRECT_PEER, {"X-Forwarded-For": header}),
            settings,
        )
        assert resolution.source == DIRECT_PEER
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"X-Forwarded-For": f"{SPOOFED}, {CLIENT_B}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_B
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "203.0.113.200",
            {"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert resolution.source == "203.0.113.200"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs="10.0.0.0/8",
        cloudflare_cidrs="173.245.55.0/24",
    )
    resolution = resolve_admin_login_client_source(
        _request(
            DIRECT_PEER,
            {
                "CF-Connecting-IP": CLIENT_A,
                "X-Forwarded-For": CLIENT_A,
            },
        ),
        settings,
    )
    assert resolution.source == DIRECT_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_header_precedence_xff_over_cf_connecting_and_forwarded() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs="10.0.0.0/8",
        cloudflare_cidrs="173.245.55.0/24",
    )
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {
                "X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}",
                "CF-Connecting-IP": CLIENT_B,
                "Forwarded": f'for={CLIENT_B};proto=https, for="{RENDER_PROXY}"',
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == "trusted_xff"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"Forwarded": f'for={CLIENT_A};proto=https, for="{RENDER_PROXY}"'},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == "trusted_forwarded"


@pytest.mark.unit
def test_address_format_edge_cases() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {
                "X-Forwarded-For": (
                    f" {CLIENT_A} , {RENDER_PROXY} , 2001:db8::2, ::ffff:203.0.113.8 "
                )
            },
        ),
        settings,
    )
    assert resolution.source == "203.0.113.8"
    assert resolution.path == "trusted_xff"

    ipv6 = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {"X-Forwarded-For": f"2001:db8::2, {RENDER_PROXY}"},
        ),
        settings,
    )
    assert ipv6.source == "2001:db8::2"

    malformed = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": " , , "}),
        settings,
    )
    assert malformed.source == RENDER_PROXY
    assert malformed.path == "malformed_xff"

    overlong = ",".join([CLIENT_A] * 40)
    too_long = resolve_admin_login_client_source(
        _request(RENDER_PROXY, {"X-Forwarded-For": overlong}),
        settings,
    )
    assert too_long.source == RENDER_PROXY
    assert too_long.path == "malformed_xff"


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    settings = _settings(trust_proxy=True, trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(_request(None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == "missing_peer"


@pytest.mark.unit
def test_cf_connecting_ip_fallback_requires_cloudflare_edge_in_xff() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs="10.0.0.0/8",
        cloudflare_cidrs="173.245.55.0/24",
    )
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            {
                "X-Forwarded-For": f"not-an-ip, {CLOUDFLARE_EDGE}, {RENDER_PROXY}",
                "CF-Connecting-IP": CLIENT_A,
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_telemetry_without_raw_ips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trust_proxy=False)
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(
            _request(DIRECT_PEER, {"X-Forwarded-For": SPOOFED}),
            settings,
        )
    assert DIRECT_PEER not in caplog.text
    assert SPOOFED not in caplog.text
    assert any(
        getattr(record, "resolution_path", None) == "untrusted_peer_forwarding"
        for record in caplog.records
    )


@pytest.mark.unit
def test_client_ip_wrapper_returns_resolution_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(RENDER_PROXY, {"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"})
    assert admin_auth.client_ip(request, settings) == CLIENT_A


@pytest.mark.unit
def test_source_rate_limit_keys_do_not_embed_raw_addresses() -> None:
    source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert len(source_key) == 64
    assert CLIENT_A not in source_key
    assert "x-forwarded-for" not in source_key
