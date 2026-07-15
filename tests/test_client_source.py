"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.client_source import (
    CLIENT_SOURCE_TRUST_MODEL,
    PATH_DIRECT_PEER,
    PATH_MALFORMED_FORWARDING,
    PATH_TRUSTED_CF_CONNECTING_IP,
    PATH_TRUSTED_FORWARDED,
    PATH_TRUSTED_XFF_RTL,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.ip_trust import normalize_client_address, parse_trusted_proxy_networks

RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
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


@pytest.fixture
def trusted_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    return get_settings()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_client_address("[2001:db8::1]:8443") == "2001:db8::1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("   ") is None


@pytest.mark.unit
def test_parse_trusted_proxy_networks_accepts_cidr_and_hosts() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8,127.0.0.1,::1")
    assert len(networks) == 3


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(trusted_settings) -> None:
    """Untrusted peer: X-Forwarded-For must not control the limiter key."""
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == PATH_DIRECT_PEER

    request_multi = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    resolution_multi = resolve_admin_login_client_source(request_multi, trusted_settings)
    assert resolution_multi.source == "198.51.100.10"
    assert resolution_multi.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_appended_client_not_leftmost(trusted_settings) -> None:
    """Attacker left-most value followed by real connecting address."""
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 198.51.100.20"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.20"
    assert resolution.path == PATH_TRUSTED_XFF_RTL


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_settings) -> None:
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == PATH_TRUSTED_XFF_RTL


@pytest.mark.unit
def test_partial_trust_fails_closed(trusted_settings) -> None:
    """Trusted proxy behind an untrusted intermediary must not parse headers."""
    request = _request_with_client(
        "198.51.100.44",
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.44"
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(trusted_settings) -> None:
    request = _request_with_client(
        "198.51.100.55",
        headers={"CF-Connecting-IP": "203.0.113.77"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.55"
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(trusted_settings) -> None:
    request = _request_with_client(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.10, 10.0.0.1",
            "Forwarded": 'for="203.0.113.99", for=10.0.0.1',
            "CF-Connecting-IP": "203.0.113.88",
        },
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path == PATH_TRUSTED_XFF_RTL
    assert resolution.header_family == "xff"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(trusted_settings) -> None:
    request = _request_with_client(
        "10.0.0.5",
        headers={"Forwarded": 'for="203.0.113.41", for=10.0.0.1'},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.41"
    assert resolution.path == PATH_TRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_fallback_when_no_chain(trusted_settings) -> None:
    request = _request_with_client(
        "10.0.0.5",
        headers={"CF-Connecting-IP": "203.0.113.60"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path == PATH_TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_is_conservative(trusted_settings) -> None:
    malformed = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    malformed_resolution = resolve_admin_login_client_source(malformed, trusted_settings)
    assert malformed_resolution.source == "10.0.0.5"
    assert malformed_resolution.path == PATH_MALFORMED_FORWARDING

    overlong = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": ", ".join(f"203.0.113.{i % 250}" for i in range(40))},
    )
    overlong_resolution = resolve_admin_login_client_source(overlong, trusted_settings)
    assert overlong_resolution.source == "10.0.0.5"
    assert overlong_resolution.path == PATH_MALFORMED_FORWARDING


@pytest.mark.unit
def test_whitespace_and_empty_elements_rejected(trusted_settings) -> None:
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": " , , "},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path == PATH_MALFORMED_FORWARDING


@pytest.mark.unit
def test_missing_peer_maps_to_unknown(trusted_settings) -> None:
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "unknown"


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    trusted_settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 198.51.100.20"},
    )
    resolve_admin_login_client_source(request, trusted_settings)
    combined = caplog.text
    assert "203.0.113.50" not in combined
    assert "198.51.100.20" not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert "source_resolution_path" not in combined  # extra dict, not message body


@pytest.mark.unit
def test_client_source_trust_model_constant() -> None:
    assert CLIENT_SOURCE_TRUST_MODEL == "verified-proxy-hop-v1"
