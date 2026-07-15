"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request

from app.admin_client_source import (
    client_from_forwarded_header,
    client_from_forwarding_chain,
    client_ip,
    normalize_client_address,
    parse_forwarded_header,
    parse_trusted_proxy_networks,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _append_header(request: Request, name: str, value: str) -> None:
    request.headers.__dict__["_list"].append((name.lower().encode(), value.encode()))


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address(" 203.0.113.1:443 ") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("2001:db8:0:0:0:0:0:1") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]:8080") == "2001:db8::1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request_with_client("198.51.100.10")
    _append_header(request, "x-forwarded-for", "203.0.113.99")
    assert client_ip(request, settings) == "198.51.100.10"

    request = _request_with_client("198.51.100.10")
    _append_header(request, "x-forwarded-for", "203.0.113.1, 203.0.113.2, 203.0.113.3")
    assert client_ip(request, settings) == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("10.0.0.5")
    _append_header(
        request,
        "x-forwarded-for",
        "203.0.113.99, 198.51.100.42",
    )
    assert client_ip(request, settings) == "198.51.100.42"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("10.0.0.5")
    _append_header(request, "cf-connecting-ip", "203.0.113.50")
    _append_header(
        request,
        "x-forwarded-for",
        "203.0.113.50, 10.0.0.5",
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("203.0.113.200")
    _append_header(request, "cf-connecting-ip", "203.0.113.50")
    _append_header(request, "x-forwarded-for", "203.0.113.50, 10.0.0.5")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.200"
    assert resolution.path == "untrusted_peer"
    assert resolution.rejected_forwarded is True


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("198.51.100.77")
    _append_header(request, "cf-connecting-ip", "203.0.113.10")
    _append_header(request, "x-forwarded-for", "203.0.113.10")
    assert client_ip(request, settings) == "198.51.100.77"


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("10.0.0.5")
    _append_header(request, "cf-connecting-ip", "203.0.113.7")
    _append_header(request, "x-forwarded-for", "203.0.113.8, 10.0.0.5")
    _append_header(request, "forwarded", 'for=203.0.113.9;proto=https')
    assert client_ip(request, settings) == "203.0.113.7"


@pytest.mark.unit
def test_xff_used_when_cf_missing_and_forwarded_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    request = _request_with_client("10.0.0.5")
    _append_header(request, "x-forwarded-for", "203.0.113.8, 10.0.0.5")
    _append_header(request, "forwarded", 'for=203.0.113.9;proto=https')
    assert client_ip(request, settings) == "203.0.113.8"


@pytest.mark.unit
def test_address_format_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()

    request = _request_with_client("10.0.0.5")
    _append_header(request, "x-forwarded-for", " 203.0.113.1 , 10.0.0.5 ")
    assert client_ip(request, settings) == "203.0.113.1"

    request = _request_with_client("10.0.0.5")
    _append_header(request, "x-forwarded-for", ", , 203.0.113.2, 10.0.0.5")
    assert client_ip(request, settings) == "203.0.113.2"

    request = _request_with_client("10.0.0.5")
    _append_header(request, "x-forwarded-for", "not-valid, 203.0.113.3, 10.0.0.5")
    assert client_ip(request, settings) == "203.0.113.3"

    request = _request_with_client("10.0.0.5")
    long_chain = ", ".join(["203.0.113.4"] * 40 + ["10.0.0.5"])
    _append_header(request, "x-forwarded-for", long_chain)
    assert client_ip(request, settings) == "203.0.113.4"


@pytest.mark.unit
def test_client_from_forwarding_chain_skips_trusted_hops() -> None:
    trusted = parse_trusted_proxy_networks(RENDER_TRUSTED_CIDRS)
    assert (
        client_from_forwarding_chain(
            "203.0.113.50, 10.0.0.5",
            trusted_networks=trusted,
        )
        == "203.0.113.50"
    )


@pytest.mark.unit
def test_parse_forwarded_header_extracts_for_values() -> None:
    assert parse_forwarded_header('for=203.0.113.1;proto=https') == ["203.0.113.1"]
    assert parse_forwarded_header('for="[2001:db8::1]:443";proto=https') == ["2001:db8::1"]


@pytest.mark.unit
def test_client_from_forwarded_header_uses_rightmost_untrusted() -> None:
    trusted = parse_trusted_proxy_networks(RENDER_TRUSTED_CIDRS)
    value = 'for=203.0.113.1;proto=https, for=10.0.0.5;proto=https'
    assert client_from_forwarded_header(value, trusted_networks=trusted) == "203.0.113.1"


@pytest.mark.unit
def test_missing_peer_uses_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/admin/login"})
    assert client_ip(request, settings) == "unknown"


@pytest.mark.unit
def test_trust_enabled_without_boundary_ignores_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client("10.0.0.5")
    _append_header(request, "cf-connecting-ip", "203.0.113.50")
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path == "unconfigured_trust_boundary"


@pytest.mark.unit
def test_rejected_forwarded_telemetry_is_sampled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    settings = get_settings()
    caplog.set_level(logging.INFO)

    for _ in range(15):
        request = _request_with_client("198.51.100.10")
        _append_header(request, "x-forwarded-for", "203.0.113.99")
        resolve_admin_login_client_source(request, settings)

    matching = [
        record
        for record in caplog.records
        if record.getMessage() == "Admin login ignored untrusted forwarding headers"
    ]
    assert len(matching) == 10
    assert all(
        "203.0.113" not in str(record.__dict__)
        and "198.51.100" not in str(record.__dict__)
        for record in matching
    )
