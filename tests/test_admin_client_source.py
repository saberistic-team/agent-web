"""Unit tests for trusted-hop admin login client source resolution."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi import Request

from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip_address,
    reset_source_telemetry_state,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_PROXY = "103.21.244.50"
CLIENT_IP = "203.0.113.10"
ATTACKER_IP = "198.51.100.20"
OTHER_CLIENT = "203.0.113.77"

TEST_TRUSTED_CIDRS = (
    "10.0.0.0/8",
    "103.21.244.0/22",
)
TEST_CLOUDFLARE_CIDRS = ("103.21.244.0/22",)


def _settings_with_trust(**overrides: object) -> Settings:
    base = replace(
        get_settings(),
        admin_trusted_proxy_cidrs=TEST_TRUSTED_CIDRS,
        admin_cloudflare_proxy_cidrs=TEST_CLOUDFLARE_CIDRS,
    )
    return replace(base, **overrides)


def _request(
    *,
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
    reset_source_telemetry_state()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored_without_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    single = _request(
        peer=ATTACKER_IP,
        headers=[_header("x-forwarded-for", "203.0.113.99")],
    )
    multi = _request(
        peer=ATTACKER_IP,
        headers=[_header("x-forwarded-for", "203.0.113.1, 203.0.113.2")],
    )
    assert resolve_admin_login_client_source(single, settings).source == ATTACKER_IP
    assert resolve_admin_login_client_source(multi, settings).source == ATTACKER_IP


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"203.0.113.99, {CLIENT_IP}, {CLOUDFLARE_PROXY}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.source != "203.0.113.99"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{CLIENT_IP}, {CLOUDFLARE_PROXY}, {RENDER_PROXY}",
            ),
            _header("cf-connecting-ip", CLIENT_IP),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_partial_trust_fails_closed_when_untrusted_intermediary() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{CLIENT_IP}, {ATTACKER_IP}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_IP
    assert resolution.path is SourceResolutionPath.UNTRUSTED_HEADERS
    assert resolution.untrusted_header_attempt is True


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_cloudflare_headers() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header("x-forwarded-for", f"203.0.113.99, {ATTACKER_IP}"),
            _header("cf-connecting-ip", "203.0.113.55"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_IP
    assert resolution.path is SourceResolutionPath.UNTRUSTED_HEADERS


@pytest.mark.unit
def test_x_forwarded_for_precedence_over_forwarded_header() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{CLIENT_IP}, {CLOUDFLARE_PROXY}, {RENDER_PROXY}",
            ),
            _header("forwarded", f'for="{OTHER_CLIENT}";proto=https'),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "forwarded",
                f'for="{CLIENT_IP}";proto=https, for="{CLOUDFLARE_PROXY}";proto=https, '
                f'for="{RENDER_PROXY}";proto=https',
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_data_is_conservative() -> None:
    settings = _settings_with_trust()
    invalid = _request(
        peer=ATTACKER_IP,
        headers=[_header("x-forwarded-for", "not-an-ip, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(invalid, settings)
    assert resolution.path is SourceResolutionPath.INVALID_FORWARDING
    assert resolution.source == ATTACKER_IP

    overlong = ", ".join([f"10.0.0.{index % 250}" for index in range(40)])
    overlong_request = _request(
        peer=ATTACKER_IP,
        headers=[_header("x-forwarded-for", overlong)],
    )
    overlong_resolution = resolve_admin_login_client_source(overlong_request, settings)
    assert overlong_resolution.path is SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_empty_xff_elements_and_whitespace_are_rejected() -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=ATTACKER_IP,
        headers=[_header("x-forwarded-for", " , , ")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path is SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    settings = _settings_with_trust()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_telemetry_without_raw_ips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trust()
    request = _request(
        peer=RENDER_PROXY,
        headers=[_header("x-forwarded-for", f"203.0.113.99, {ATTACKER_IP}")],
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
        resolve_admin_login_client_source(request, settings)
    messages = [record.message for record in caplog.records]
    assert any("forwarding rejected" in message for message in messages)
    joined = " ".join(messages)
    assert ATTACKER_IP not in joined
    assert "203.0.113.99" not in joined
    assert "x-forwarded-for" not in joined.lower()


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_enables_production_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    assert settings.admin_trusted_proxy_cidrs
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header(
                "x-forwarded-for",
                f"{CLIENT_IP}, {CLOUDFLARE_PROXY}, {RENDER_PROXY}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_IP
