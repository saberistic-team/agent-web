"""Unit tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Iterable

import pytest
from fastapi import Request

from app.admin_client_source import (
    normalize_ip_address,
    record_client_source_telemetry,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_PROXY = "10.0.0.5"
CLIENT_IP = "203.0.113.50"
ATTACKER_IP = "198.51.100.99"
CLOUDFLARE_EDGE = "104.16.132.229"


def _settings(
    *,
    trusted_cidrs: Iterable[str] = (),
    trust_proxy_headers: bool = False,
) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_trusted_proxy_cidrs=tuple(trusted_cidrs),
        admin_trust_proxy_headers=trust_proxy_headers,
    )


def _request(
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.2 ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.999.999", None),
    ],
)
def test_normalize_ip_address(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings()
    for header_value in (
        ATTACKER_IP,
        f"{ATTACKER_IP}, {CLIENT_IP}",
    ):
        request = _request(
            CLIENT_IP,
            headers=[_header("x-forwarded-for", header_value)],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == CLIENT_IP
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_untrusted_peer_with_forwarding_headers_fails_closed() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        ATTACKER_IP,
        headers=[_header("x-forwarded-for", f"{CLIENT_IP}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "direct_untrusted_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{ATTACKER_IP}, {CLIENT_IP}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.path == "trusted_x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{CLIENT_IP}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        "203.0.113.77",
        headers=[_header("x-forwarded-for", f"{CLIENT_IP}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "direct_untrusted_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header("cf-connecting-ip", ATTACKER_IP),
            _header("x-real-ip", ATTACKER_IP),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "unknown"


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header("x-forwarded-for", f"{CLIENT_IP}, {RENDER_PROXY}"),
            _header("forwarded", f'for="{ATTACKER_IP}"'),
            _header("cf-connecting-ip", ATTACKER_IP),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.path == "trusted_x_forwarded_for"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[_header("forwarded", f'for="{CLIENT_IP}";proto=https')],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.path == "trusted_forwarded"


@pytest.mark.unit
def test_address_format_edge_cases() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f" , ::ffff:{CLIENT_IP}, {RENDER_PROXY} ",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP

    invalid = _request(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", "not-an-ip, 10.0.0.5")],
    )
    assert resolve_admin_login_client_source(invalid, settings).path == "invalid_forwarding"

    overlong = _request(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", ", ".join(["1.2.3.4"] * 40))],
    )
    assert resolve_admin_login_client_source(overlong, settings).path == "invalid_forwarding"


@pytest.mark.unit
def test_duplicate_and_empty_xff_elements() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f", {CLIENT_IP}, , {CLIENT_IP}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP


@pytest.mark.unit
def test_legacy_trust_flag_uses_render_defaults() -> None:
    settings = _settings(trust_proxy_headers=True)
    request = _request(
        RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"{CLIENT_IP}, {RENDER_PROXY}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    settings = _settings()
    request = _request(
        CLIENT_IP,
        headers=[_header("x-forwarded-for", ATTACKER_IP)],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    record_client_source_telemetry(resolution)
    combined = caplog.text
    assert ATTACKER_IP not in combined
    assert CLIENT_IP not in combined
    assert "client_source_path" not in combined
    assert any(record.message == "Admin login client source resolved" for record in caplog.records)


@pytest.mark.unit
def test_cloudflare_hop_in_chain_allows_parsing() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
