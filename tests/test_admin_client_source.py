"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request

from app.admin_client_source import (
    DEFAULT_RENDER_TRUSTED_PROXY_CIDRS,
    RESOLUTION_PATH_CF_CONNECTING_IP,
    RESOLUTION_PATH_DIRECT_PEER,
    RESOLUTION_PATH_FORWARDED_FOR,
    RESOLUTION_PATH_FORWARDED_HEADER,
    RESOLUTION_PATH_UNKNOWN,
    RESOLUTION_PATH_UNTRUSTED_FORWARDING,
    normalize_client_source,
    reset_untrusted_forwarding_telemetry,
    resolve_admin_login_client_source,
    resolve_admin_login_client_source_detail,
)
from app.config import Settings, get_settings


def _request(
    *,
    peer: str | None = "198.51.100.10",
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


def _settings(
    *,
    trusted_cidrs: tuple[str, ...] = (),
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
        admin_trust_proxy_headers=trust_proxy_headers,
        admin_trusted_proxy_cidrs=trusted_cidrs,
        uvicorn_forwarded_allow_ips=",".join(trusted_cidrs),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "::ffff:203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("x" * 300, None),
    ],
)
def test_normalize_client_source(raw: str, expected: str | None) -> None:
    assert normalize_client_source(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings()
    single = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    multi = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    assert resolve_admin_login_client_source(single, settings) == "198.51.100.10"
    assert resolve_admin_login_client_source(multi, settings) == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b"203.0.113.99, 203.0.113.50")],
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.50"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8", "172.16.0.0/12"))
    request = _request(
        peer="10.1.2.3",
        headers=[(b"x-forwarded-for", b"203.0.113.77, 172.16.5.6")],
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.77"


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.77, 10.0.0.1")],
    )
    assert resolve_admin_login_client_source(request, settings) == "198.51.100.10"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        peer="198.51.100.10",
        headers=[
            (b"cf-connecting-ip", b"203.0.113.88"),
            (b"cf-ray", b"abc123"),
            (b"x-forwarded-for", b"203.0.113.88"),
        ],
    )
    assert resolve_admin_login_client_source(request, settings) == "198.51.100.10"


@pytest.mark.unit
def test_cf_connecting_ip_precedence_when_cloudflare_edge_proven() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        peer="10.0.0.1",
        headers=[
            (b"cf-connecting-ip", b"203.0.113.44"),
            (b"cf-ray", b"7c1f2a3b4d5e6f78-SJC"),
            (b"x-forwarded-for", b"203.0.113.99, 203.0.113.44"),
            (b"forwarded", b'for="203.0.113.55";proto=https'),
        ],
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == RESOLUTION_PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(
        peer="10.0.0.1",
        headers=[(b"forwarded", b'for="203.0.113.60";proto=https, for=10.0.0.1')],
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path == RESOLUTION_PATH_FORWARDED_HEADER


@pytest.mark.unit
def test_address_format_edge_cases_are_deterministic() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    ipv6 = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b"[2001:db8::5], 10.0.0.1")],
    )
    mapped = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b"::ffff:203.0.113.5, 10.0.0.1")],
    )
    empty_elements = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b" , 203.0.113.5, ,10.0.0.1")],
    )
    invalid = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b"not-valid, 10.0.0.1")],
    )
    overlong = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", ", ".join(f"10.0.0.{i}" for i in range(40)).encode())],
    )

    assert resolve_admin_login_client_source(ipv6, settings) == "2001:db8::5"
    assert resolve_admin_login_client_source(mapped, settings) == "::ffff:203.0.113.5"
    assert resolve_admin_login_client_source(empty_elements, settings) == "203.0.113.5"
    assert resolve_admin_login_client_source(invalid, settings) == "unknown"
    assert resolve_admin_login_client_source(overlong, settings) == "unknown"


@pytest.mark.unit
def test_missing_peer_resolves_unknown() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    assert resolve_admin_login_client_source(_request(peer=None), settings) == "unknown"


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_applies_render_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request(
        peer="10.0.0.1",
        headers=[(b"x-forwarded-for", b"203.0.113.99, 203.0.113.50")],
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.50"
    assert DEFAULT_RENDER_TRUSTED_PROXY_CIDRS


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_telemetry(caplog: pytest.LogCaptureFixture) -> None:
    reset_untrusted_forwarding_telemetry()
    settings = _settings()
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.1")],
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        first = resolve_admin_login_client_source_detail(request, settings)
        second = resolve_admin_login_client_source_detail(request, settings)
    assert first.path == RESOLUTION_PATH_DIRECT_PEER
    assert second.path == RESOLUTION_PATH_DIRECT_PEER
    assert any(
        record.getMessage() == "Admin login source resolution ignored untrusted forwarding headers"
        and record.__dict__.get("source_resolution_path") == RESOLUTION_PATH_UNTRUSTED_FORWARDING
        for record in caplog.records
    )


@pytest.mark.unit
def test_trusted_peer_without_headers_uses_unknown() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request(peer="10.0.0.1")
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == RESOLUTION_PATH_UNKNOWN
