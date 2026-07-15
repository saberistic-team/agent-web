"""Unit tests for trusted admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourceResolution,
    normalize_ip_address,
    reset_invalid_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_PEER = "10.0.0.5"
RENDER_TRUSTED_CIDRS = "10.0.0.0/8"
CLOUDFLARE_TEST_IP = "198.41.192.7"
CLOUDFLARE_TEST_CIDRS = "198.41.192.0/24"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "203.0.113.99"


def _request(
    *,
    peer: str | None = RENDER_PEER,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 54321)
    request = Request(scope)
    if headers:
        for name, value in headers.items():
            request.headers.__dict__["_list"].append(
                (name.lower().encode("ascii"), value.encode("ascii"))
            )
    return request


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", CLOUDFLARE_TEST_CIDRS)
    reset_invalid_forwarding_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = get_settings()
    for header in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_TEST_IP}",
    ):
        request = _request(peer="198.51.100.10", headers={"X-Forwarded-For": header})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution == ClientSourceResolution("198.51.100.10", "direct_peer")


@pytest.mark.unit
def test_cloudflare_append_preserves_real_client_not_leftmost() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_TEST_IP}",
        }
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request(
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_TEST_IP}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = get_settings()
    request = _request(
        peer="198.51.100.20",
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PEER}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.20"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_vendor_headers() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": REAL_CLIENT,
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "True-Client-IP": SPOOFED_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_chain"


@pytest.mark.unit
def test_vendor_header_used_when_cloudflare_hop_proven() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_TEST_IP}",
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_chain"


@pytest.mark.unit
def test_cf_connecting_ip_fallback_when_forwarding_headers_absent() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": CLOUDFLARE_TEST_IP,
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_multiple_header_families_precedence_prefers_valid_xff() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_TEST_IP}",
            "Forwarded": 'for=203.0.113.1;proto=https, for="[2001:db8::9]";proto=https',
            "CF-Connecting-IP": "203.0.113.1",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_chain"


@pytest.mark.unit
def test_rfc7239_forwarded_used_when_xff_missing() -> None:
    settings = get_settings()
    request = _request(
        headers={
            "Forwarded": (
                f'for={REAL_CLIENT};proto=https, for={CLOUDFLARE_TEST_IP};proto=https'
            ),
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path == "forwarded_rfc7239"


@pytest.mark.unit
def test_malformed_and_overlong_chains_fall_back_to_peer() -> None:
    settings = get_settings()
    malformed = _request(headers={"X-Forwarded-For": " , "})
    assert resolve_admin_login_client_source(malformed, settings).path == "peer_fallback"

    overlong = ",".join([f"203.0.113.{index}" for index in range(40)])
    request = _request(headers={"X-Forwarded-For": overlong})
    assert resolve_admin_login_client_source(request, settings).path == "peer_fallback"


@pytest.mark.unit
def test_missing_peer_is_unknown() -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(_request(peer=None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == "unknown_peer"


@pytest.mark.unit
def test_proxy_trust_disabled_ignores_all_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "false")
    settings = get_settings()
    request = _request(
        headers={
            "X-Forwarded-For": SPOOFED_CLIENT,
            "CF-Connecting-IP": SPOOFED_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_invalid_forwarding_telemetry_is_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO)
    for _ in range(5):
        resolve_admin_login_client_source(
            _request(peer="198.51.100.50", headers={"X-Forwarded-For": SPOOFED_CLIENT}),
            settings,
        )
    rejection_logs = [
        record
        for record in caplog.records
        if record.message == "Admin login source forwarding rejected"
    ]
    assert len(rejection_logs) == 1
    assert rejection_logs[0].source_resolution_reason == "untrusted_peer"


@pytest.mark.unit
def test_resolution_telemetry_contains_no_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.admin_client_source import log_admin_login_source_resolution

    caplog.set_level(logging.INFO)
    resolution = ClientSourceResolution(REAL_CLIENT, "forwarded_chain")
    log_admin_login_source_resolution(resolution)
    record = caplog.records[-1]
    assert record.source_resolution_path == "forwarded_chain"
    assert REAL_CLIENT not in record.getMessage()
    for field in ("source", "ip", "xff", "forwarded"):
        assert field not in record.__dict__
