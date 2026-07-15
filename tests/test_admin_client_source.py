"""Unit tests for trusted admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip_address,
    reset_source_resolution_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.proxy_trust import parse_trusted_proxy_networks
from app.config import get_settings

RENDER_PROXY = "10.0.0.5"
CLOUDFLARE_EDGE = "198.51.100.20"
CLIENT = "203.0.113.50"
ATTACKER = "198.51.100.10"
UNTRUSTED_INTERMEDIARY = "203.0.113.99"

TRUSTED_CIDRS = "10.0.0.0/8"
CLOUDFLARE_CIDRS = "198.51.100.0/24"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_CIDRS", CLOUDFLARE_CIDRS)
    reset_source_resolution_telemetry_for_tests()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        (" 2001:db8::1 ", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("x" * 200, None),
    ],
)
def test_normalize_ip_address(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored() -> None:
    settings = get_settings()
    request = _request_with_client(
        ATTACKER,
        headers={"X-Forwarded-For": CLIENT},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == ATTACKER
    assert result.path == SourceResolutionPath.DIRECT_PEER
    assert result.invalid_forwarding is True


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored() -> None:
    settings = get_settings()
    request = _request_with_client(
        ATTACKER,
        headers={"X-Forwarded-For": f"{CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == ATTACKER
    assert result.path == SourceResolutionPath.DIRECT_PEER
    assert result.invalid_forwarding is True


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    settings = get_settings()
    attacker_real_ip = "203.0.113.250"
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"203.0.113.1, {attacker_real_ip}"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == attacker_real_ip
    assert result.path == SourceResolutionPath.X_FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == CLIENT
    assert result.path == SourceResolutionPath.X_FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed_to_untrusted_hop() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {UNTRUSTED_INTERMEDIARY}"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == UNTRUSTED_INTERMEDIARY
    assert result.path == SourceResolutionPath.X_FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip_without_cf_hop() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": CLIENT,
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.path != SourceResolutionPath.CF_CONNECTING_IP_VERIFIED
    assert result.address == CLIENT


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_verified() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"{ATTACKER}, {CLOUDFLARE_EDGE}",
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == CLIENT
    assert result.path == SourceResolutionPath.CF_CONNECTING_IP_VERIFIED


@pytest.mark.unit
def test_header_precedence_prefers_verified_cf_connecting_ip() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"{ATTACKER}, {CLOUDFLARE_EDGE}",
            "Forwarded": f'for="{ATTACKER}";proto=https',
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == CLIENT
    assert result.path == SourceResolutionPath.CF_CONNECTING_IP_VERIFIED


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"Forwarded": f'for="{CLIENT}";proto=https'},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == CLIENT
    assert result.path == SourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
def test_malformed_xff_falls_back_to_peer() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == RENDER_PROXY
    assert result.path == SourceResolutionPath.INVALID_FORWARDING
    assert result.invalid_forwarding is True


@pytest.mark.unit
def test_overlong_xff_chain_fails_closed() -> None:
    settings = get_settings()
    chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    request = _request_with_client(RENDER_PROXY, headers={"X-Forwarded-For": chain})
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == RENDER_PROXY
    assert result.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    settings = get_settings()
    request = _request_with_client("")
    request.scope["client"] = None
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "unknown"
    assert result.path == SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_telemetry_emits_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    request = _request_with_client(
        ATTACKER,
        headers={"X-Forwarded-For": CLIENT},
    )
    resolve_admin_login_client_source(request, settings)
    assert CLIENT not in caplog.text
    assert ATTACKER not in caplog.text
    assert any(
        record.__dict__.get("admin_login_invalid_forwarding") is True
        for record in caplog.records
    )


@pytest.mark.unit
def test_xff_empty_elements_are_skipped() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f" {CLIENT} , , {CLOUDFLARE_EDGE} "},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == CLIENT


@pytest.mark.unit
def test_peer_hostname_fallback_for_non_ip_test_clients() -> None:
    settings = get_settings()
    request = _request_with_client("testclient")
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "testclient"
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_parse_trusted_proxy_networks_accepts_hosts_and_cidrs() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8, 127.0.0.1")
    assert len(networks) == 2
