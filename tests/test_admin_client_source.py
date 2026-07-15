"""Unit tests for trusted admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    PATH_CF_CONNECTING_IP,
    PATH_DIRECT_PEER,
    PATH_FORWARDED_CHAIN,
    PATH_FORWARDED_HEADER,
    PATH_INVALID_FORWARDED,
    PATH_UNTRUSTED_FORWARDED,
    normalize_client_address,
    reset_trusted_network_cache,
    resolve_admin_login_client_source,
    resolve_admin_login_client_source_detail,
    reset_source_resolution_telemetry,
)
from app.config import get_settings

RENDER_PROXY = "10.0.0.1"
CF_EDGE = "198.51.100.99"
CLIENT = "203.0.113.50"
UNTRUSTED_PEER = "198.51.100.10"

PROXY_CIDRS = "10.0.0.0/8"
EDGE_CIDRS = "198.51.100.0/24"


def _request(
    *,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("latin1"), value.encode("latin1"))
        for name, value in (headers or {}).items()
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


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", EDGE_CIDRS)
    reset_trusted_network_cache()
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("2001:0db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:8443") == "2001:db8::1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = get_settings()
    for header in (
        "203.0.113.99",
        "203.0.113.99, 203.0.113.1, 10.0.0.1",
    ):
        request = _request(
            peer=UNTRUSTED_PEER,
            headers={"X-Forwarded-For": header},
        )
        detail = resolve_admin_login_client_source_detail(request, settings)
        assert detail.source == UNTRUSTED_PEER
        assert detail.path == PATH_UNTRUSTED_FORWARDED


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"203.0.113.99, {CLIENT}, {CF_EDGE}",
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == CLIENT
    assert detail.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {CF_EDGE}"},
    )
    assert resolve_admin_login_client_source(request, settings) == CLIENT


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_not_client_leftmost() -> None:
    settings = get_settings()
    intermediary = "203.0.113.77"
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {intermediary}"},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == intermediary
    assert detail.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"CF-Connecting-IP": "203.0.113.99"},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == RENDER_PROXY
    assert detail.path == PATH_UNTRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_verified() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT}, {CF_EDGE}",
            "CF-Connecting-IP": CLIENT,
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == CLIENT
    assert detail.path == PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_cf_over_forwarded_and_xff() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"203.0.113.88, {CF_EDGE}",
            "CF-Connecting-IP": CLIENT,
            "Forwarded": 'for="203.0.113.77"',
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == CLIENT
    assert detail.path == PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_when_cf_unverified() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT}, 10.0.0.2",
            "CF-Connecting-IP": "203.0.113.99",
            "Forwarded": 'for="203.0.113.77"',
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == CLIENT
    assert detail.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_only_forwarding_family_present() -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"Forwarded": f'for="{CLIENT}";proto=https'},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == CLIENT
    assert detail.path == PATH_FORWARDED_HEADER


@pytest.mark.unit
def test_invalid_and_overlong_chains_fail_closed_to_peer() -> None:
    settings = get_settings()
    invalid = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.2"},
    )
    assert resolve_admin_login_client_source_detail(invalid, settings).path == PATH_INVALID_FORWARDED

    overlong = ", ".join([f"10.0.0.{index}" for index in range(40)])
    request = _request(peer=RENDER_PROXY, headers={"X-Forwarded-For": overlong})
    assert resolve_admin_login_client_source_detail(request, settings).path == PATH_INVALID_FORWARDED


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    settings = get_settings()
    request = _request(peer=None)
    assert resolve_admin_login_client_source(request, settings) == "unknown"


@pytest.mark.unit
def test_no_trusted_proxy_cidrs_ignores_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    reset_trusted_network_cache()
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": CLIENT},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == RENDER_PROXY
    assert detail.path == PATH_UNTRUSTED_FORWARDED


@pytest.mark.unit
def test_telemetry_emits_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.DEBUG)
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolve_admin_login_client_source(request, settings)
    messages = " ".join(record.message for record in caplog.records)
    assert "203.0.113.99" not in messages
    assert any(
        getattr(record, "source_resolution_path", None) == PATH_UNTRUSTED_FORWARDED
        for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_and_app_edge_trust_differ() -> None:
    """Render-only Uvicorn trust stops at the edge hop; app edge CIDRs reach the client."""
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    settings = get_settings()
    chain = f"203.0.113.99, {CLIENT}, {CF_EDGE}"
    render_only = _TrustedHosts([PROXY_CIDRS])
    uvicorn_host, _port = render_only.get_trusted_client_address(chain)
    assert uvicorn_host == CF_EDGE
    request = _request(
        peer=RENDER_PROXY,
        headers={"X-Forwarded-For": chain},
    )
    assert resolve_admin_login_client_source(request, settings) == CLIENT
