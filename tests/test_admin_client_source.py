"""Unit tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request

from app.admin_client_source import (
    PATH_DIRECT_PEER,
    PATH_MALFORMED_FORWARDING,
    PATH_TRUSTED_FORWARDED,
    PATH_TRUSTED_XFF,
    PATH_UNTRUSTED_PEER,
    AdminClientSourceResult,
    client_ip,
    normalize_ip_address,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_PROXY = "10.0.0.1"
REAL_CLIENT = "198.51.100.10"
SPOOFED_CLIENT = "203.0.113.99"
OTHER_CLIENT = "203.0.113.77"


def _settings(
    *,
    trust_proxy: bool = False,
    trusted_proxies: tuple[str, ...] = (),
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
        admin_trust_proxy_headers=trust_proxy,
        admin_trusted_proxy_ips=trusted_proxies,
    )


def _request(
    peer: str | None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": None if peer is None else (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.50", "203.0.113.50"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113", None),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored() -> None:
    settings = _settings(trust_proxy=False)
    request = _request(
        REAL_CLIENT,
        headers=[_header("X-Forwarded-For", SPOOFED_CLIENT)],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result == AdminClientSourceResult(REAL_CLIENT, PATH_DIRECT_PEER)
    assert client_ip(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored() -> None:
    settings = _settings(trust_proxy=False)
    request = _request(
        REAL_CLIENT,
        headers=[_header("X-Forwarded-For", f"{SPOOFED_CLIENT}, {OTHER_CLIENT}")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_leftmost_spoof() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("X-Forwarded-For", f"{SPOOFED_CLIENT}, {REAL_CLIENT}")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == PATH_TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("X-Forwarded-For", REAL_CLIENT)],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == PATH_TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY,))
    request = _request(
        "203.0.113.250",
        headers=[_header("X-Forwarded-For", f"{REAL_CLIENT}, {RENDER_PROXY}")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "203.0.113.250"
    assert result.path == PATH_UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("CF-Connecting-IP", SPOOFED_CLIENT)],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == PATH_UNTRUSTED_PEER


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[
            _header("X-Forwarded-For", REAL_CLIENT),
            _header("Forwarded", f'for="{SPOOFED_CLIENT}"'),
            _header("CF-Connecting-IP", SPOOFED_CLIENT),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == PATH_TRUSTED_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("Forwarded", f'for="{REAL_CLIENT}"')],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == PATH_TRUSTED_FORWARDED


@pytest.mark.unit
def test_malformed_xff_falls_back_to_peer() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("X-Forwarded-For", "not-an-ip")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == PATH_MALFORMED_FORWARDING


@pytest.mark.unit
def test_overlong_forwarding_chain_falls_back_to_peer() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    hops = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(RENDER_PROXY, headers=[_header("X-Forwarded-For", hops)])
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == PATH_MALFORMED_FORWARDING


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY,))
    result = resolve_admin_login_client_source(_request(None), settings)
    assert result.source == "unknown"


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trust_proxy=True, trusted_proxies=(RENDER_PROXY, "10.0.0.0/8"))
    request = _request(
        RENDER_PROXY,
        headers=[_header("X-Forwarded-For", f"{SPOOFED_CLIENT}, {REAL_CLIENT}")],
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    messages = " ".join(record.message for record in caplog.records)
    assert REAL_CLIENT not in messages
    assert SPOOFED_CLIENT not in messages
    assert any(
        record.__dict__.get("source_resolution_path") == PATH_TRUSTED_XFF
        for record in caplog.records
    )
