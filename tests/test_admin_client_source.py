"""Unit tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    RESOLUTION_DIRECT_PEER,
    RESOLUTION_MALFORMED_FORWARDING,
    RESOLUTION_PROXY_TRUST_DISABLED,
    RESOLUTION_TRUSTED_CF_CONNECTING,
    RESOLUTION_TRUSTED_FORWARDED_CHAIN,
    RESOLUTION_TRUSTED_XFF_CHAIN,
    normalize_client_address,
    parse_trusted_proxy_networks,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: dict[str, str] | None = None,
) -> Request:
    header_list: list[tuple[bytes, bytes]] = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode("ascii"), value.encode("ascii")))
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
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:8443") == "2001:db8::1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "198.51.100.10"
    assert result.resolution_path == RESOLUTION_PROXY_TRUST_DISABLED

    request_multi = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    result_multi = resolve_admin_login_client_source(request_multi, settings)
    assert result_multi.address == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99, 203.0.113.50"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.50"
    assert result.resolution_path == RESOLUTION_TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8,172.16.0.0/12")
    settings = get_settings()
    request = _request(
        peer="10.1.0.1",
        headers={"X-Forwarded-For": "203.0.113.77, 172.16.5.5"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.77"
    assert result.resolution_path == RESOLUTION_TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_one_trusted_proxy_behind_untrusted_peer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="203.0.113.5",
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.1"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.5"
    assert result.resolution_path == RESOLUTION_DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="198.51.100.44",
        headers={
            "CF-Connecting-IP": "203.0.113.99",
            "X-Forwarded-For": "203.0.113.99",
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "198.51.100.44"
    assert result.resolution_path == RESOLUTION_DIRECT_PEER


@pytest.mark.unit
def test_trusted_peer_prefers_cf_connecting_ip_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "CF-Connecting-IP": "203.0.113.10",
            "X-Forwarded-For": "203.0.113.99, 203.0.113.11",
            "Forwarded": 'for="203.0.113.12"',
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.10"
    assert result.resolution_path == RESOLUTION_TRUSTED_CF_CONNECTING


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="10.0.0.3",
        headers={"Forwarded": 'for="203.0.113.20";proto=https, for=10.0.0.3'},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.20"
    assert result.resolution_path == RESOLUTION_TRUSTED_FORWARDED_CHAIN


@pytest.mark.unit
def test_address_formats_whitespace_empty_invalid_and_overlong_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()

    whitespace = _request(
        peer="10.0.0.4",
        headers={"X-Forwarded-For": "  203.0.113.1  , , 10.0.0.4"},
    )
    assert resolve_admin_login_client_source(whitespace, settings).address == "203.0.113.1"

    invalid = _request(
        peer="10.0.0.4",
        headers={"X-Forwarded-For": "not-valid, 203.0.113.2"},
    )
    assert resolve_admin_login_client_source(invalid, settings).address == "203.0.113.2"

    overlong = ",".join(["203.0.113.1"] * 40)
    overlong_request = _request(peer="10.0.0.4", headers={"X-Forwarded-For": overlong})
    overlong_result = resolve_admin_login_client_source(overlong_request, settings)
    assert overlong_result.address == "10.0.0.4"
    assert overlong_result.resolution_path == RESOLUTION_MALFORMED_FORWARDING


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="203.0.113.5",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    assert "203.0.113" not in caplog.text
    assert "resolution_path" in caplog.text or "Admin login client source resolved" in caplog.text


@pytest.mark.unit
def test_parse_trusted_proxy_networks_ignores_invalid_entries() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8,not-a-cidr,172.16.0.0/12")
    assert len(networks) == 2


@pytest.mark.unit
def test_limiter_keys_never_store_raw_forwarding_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.99, 203.0.113.50"},
    )
    source = resolve_admin_login_client_source(request, settings).address
    from app.admin_auth import build_source_rate_limit_key

    key = build_source_rate_limit_key(source)
    assert "203.0.113" not in key
    assert len(key) == 64

