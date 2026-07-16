"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.client_source import (
    ClientSourceResult,
    SourceResolutionPath,
    emit_client_source_telemetry,
    normalize_client_address,
    resolve_admin_login_client_source,
    resolve_client_source,
    trusted_networks_from_settings,
)
from app.config import Settings, get_settings

RENDER_PEER = "10.0.0.1"
CF_EDGE = "172.68.1.1"
CLIENT_IP = "203.0.113.77"
ATTACKER_PEER = "198.51.100.10"
SPOOFED_CLIENT = "203.0.113.50"


def _settings(
    *,
    proxy_cidrs: str = "10.0.0.0/8",
    edge_cidrs: str = "172.68.0.0/16",
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
        app_environment=base.app_environment,
        admin_preview_mode=base.admin_preview_mode,
        admin_preview_enabled=base.admin_preview_enabled,
        server_bind_host=base.server_bind_host,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_trusted_proxy_cidrs=proxy_cidrs,
        admin_trusted_edge_cidrs=edge_cidrs,
    )


def _request(
    *,
    peer: str | None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.fixture
def trusted_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", "172.68.0.0/16")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return _settings()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1:abc", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=ATTACKER_PEER,
        headers=[(b"x-forwarded-for", SPOOFED_CLIENT.encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == ATTACKER_PEER
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=ATTACKER_PEER,
        headers=[(b"x-forwarded-for", f"{SPOOFED_CLIENT}, {CLIENT_IP}".encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == ATTACKER_PEER
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_CLIENT}, {ATTACKER_PEER}".encode(),
            )
        ],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == RENDER_PEER
    assert result.path == SourceResolutionPath.UNTRUSTED_FORWARDING
    assert result.source != SPOOFED_CLIENT


@pytest.mark.unit
def test_trusted_chain_resolves_client(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"x-forwarded-for", f"{CLIENT_IP}, {CF_EDGE}".encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == CLIENT_IP
    assert result.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"x-forwarded-for", f"{CLIENT_IP}, {ATTACKER_PEER}".encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == RENDER_PEER
    assert result.path == SourceResolutionPath.UNTRUSTED_FORWARDING


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=ATTACKER_PEER,
        headers=[(b"cf-connecting-ip", CLIENT_IP.encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == ATTACKER_PEER
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cf_connecting_ip_requires_edge_hop_in_xff(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_IP}, {CF_EDGE}".encode()),
            (b"cf-connecting-ip", CLIENT_IP.encode()),
        ],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == CLIENT_IP
    assert result.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_without_edge_proof_fails_closed(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", CLIENT_IP.encode()),
        ],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == RENDER_PEER
    assert result.path == SourceResolutionPath.UNTRUSTED_FORWARDING


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"forwarded", f'for="{CLIENT_IP}";proto=https, for={CF_EDGE}'.encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == CLIENT_IP
    assert result.path == SourceResolutionPath.TRUSTED_FORWARDED_HEADER


@pytest.mark.unit
def test_xff_precedence_over_conflicting_forwarded(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_IP}, {CF_EDGE}".encode()),
            (b"forwarded", f'for="{SPOOFED_CLIENT}";proto=https, for={CF_EDGE}'.encode()),
        ],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == CLIENT_IP
    assert result.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_missing_peer_uses_unknown() -> None:
    settings = _settings(proxy_cidrs="", edge_cidrs="")
    result = resolve_client_source(_request(peer=None), settings)
    assert result.source == "unknown"
    assert result.path == SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_empty_cidr_lists_ignore_forwarding_headers() -> None:
    settings = _settings(proxy_cidrs="", edge_cidrs="")
    request = _request(
        peer=ATTACKER_PEER,
        headers=[(b"x-forwarded-for", SPOOFED_CLIENT.encode())],
    )
    result = resolve_client_source(request, settings)
    assert result.source == ATTACKER_PEER
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_invalid_xff_fails_closed_to_peer(trusted_proxy_settings: Settings) -> None:
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"x-forwarded-for", b"not-an-ip")],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == RENDER_PEER
    assert result.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_overlong_xff_chain_fails_closed(trusted_proxy_settings: Settings) -> None:
    chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"x-forwarded-for", chain.encode())],
    )
    result = resolve_client_source(request, trusted_proxy_settings)
    assert result.source == RENDER_PEER
    assert result.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_telemetry_emits_resolution_path_only(
    trusted_proxy_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    emit_client_source_telemetry(
        ClientSourceResult(
            source=CLIENT_IP,
            path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
        )
    )
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == "Admin login client source resolved"
    assert record.resolution_path == "trusted_xff_chain"
    assert CLIENT_IP not in record.getMessage()
    assert "x-forwarded-for" not in record.getMessage().lower()


@pytest.mark.unit
def test_trusted_networks_parse_ipv6_edge_cidr() -> None:
    settings = _settings(proxy_cidrs="10.0.0.0/8", edge_cidrs="2400:cb00::/32")
    trusted = trusted_networks_from_settings(settings)
    assert len(trusted.networks) == 1
    assert len(trusted.edge_networks) == 1


@pytest.mark.unit
def test_resolve_admin_login_client_source_ipv6_client(trusted_proxy_settings: Settings) -> None:
    ipv6_client = "2001:db8::5"
    request = _request(
        peer=RENDER_PEER,
        headers=[(b"x-forwarded-for", f"{ipv6_client}, {CF_EDGE}".encode())],
    )
    result = resolve_admin_login_client_source(request, trusted_proxy_settings)
    assert result.source == ipv6_client
    assert result.path == SourceResolutionPath.TRUSTED_XFF_CHAIN
