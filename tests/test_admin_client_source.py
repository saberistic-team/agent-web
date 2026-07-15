"""Tests for trusted-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    AdminClientSourcePath,
    normalize_ip_address,
    parse_forwarded_header,
    parse_x_forwarded_for,
    reset_invalid_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "173.245.48.1"
CLIENT_IP = "203.0.113.50"
ATTACKER_SPOOF = "203.0.113.99"
DIRECT_PEER = "198.51.100.10"

TRUSTED_CIDRS = "10.0.0.0/8,127.0.0.1/32"
CLOUDFLARE_CIDRS = "173.245.48.0/20"


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_invalid_forwarding_telemetry()
    admin_auth.reset_login_rate_limiter()


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_cidrs: str = TRUSTED_CIDRS,
    cloudflare_cidrs: str = "",
    legacy_trust: bool = False,
) -> Any:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    if legacy_trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    if trusted_cidrs:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_cidrs)
    else:
        monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    if cloudflare_cidrs:
        monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", cloudflare_cidrs)
    else:
        monkeypatch.delenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", raising=False)
    return get_settings()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::1]") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_parse_x_forwarded_for_skips_empty_and_whitespace() -> None:
    chain = parse_x_forwarded_for(" 203.0.113.1 , , 10.0.0.1 ")
    assert chain == ("203.0.113.1", "10.0.0.1")


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", ATTACKER_SPOOF.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == DIRECT_PEER
    assert result.path == AdminClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", f"{ATTACKER_SPOOF}, {CLIENT_IP}".encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == DIRECT_PEER
    assert result.path == AdminClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_appended_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{ATTACKER_SPOOF}, {CLIENT_IP}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP
    assert result.path == AdminClientSourcePath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    xff = f"{CLIENT_IP}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP
    assert result.path == AdminClientSourcePath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    untrusted = "203.0.113.77"
    xff = f"{CLIENT_IP}, {untrusted}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == untrusted
    assert result.path == AdminClientSourcePath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, cloudflare_cidrs=CLOUDFLARE_CIDRS)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"cf-connecting-ip", CLIENT_IP.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == DIRECT_PEER
    assert result.path == AdminClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_cf_connecting_ip_requires_verified_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, cloudflare_cidrs=CLOUDFLARE_CIDRS)
    xff = f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", xff.encode()),
            (b"cf-connecting-ip", CLIENT_IP.encode()),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP
    assert result.path == AdminClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_conflicting_cf_connecting_ip_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, cloudflare_cidrs=CLOUDFLARE_CIDRS)
    xff = f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", xff.encode()),
            (b"cf-connecting-ip", ATTACKER_SPOOF.encode()),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == AdminClientSourcePath.INVALID_FORWARDING


@pytest.mark.unit
def test_forwarded_header_precedence_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    forwarded = f'for={CLIENT_IP};proto=https, for={RENDER_PROXY}'
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"forwarded", forwarded.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP
    assert result.path == AdminClientSourcePath.FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
def test_xff_takes_precedence_over_forwarded_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"{CLIENT_IP}, {RENDER_PROXY}"
    forwarded = f'for={ATTACKER_SPOOF};proto=https, for={RENDER_PROXY}'
    request = _request_with_client(
        RENDER_PROXY,
        headers=[
            (b"x-forwarded-for", xff.encode()),
            (b"forwarded", forwarded.encode()),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP
    assert result.path == AdminClientSourcePath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_overlong_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    hops = [f"203.0.113.{index}" for index in range(34)]
    hops[-1] = RENDER_PROXY
    xff = ", ".join(hops)
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == AdminClientSourcePath.INVALID_FORWARDING


@pytest.mark.unit
def test_invalid_addresses_in_chain_skipped_or_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    xff = f"not-an-ip, {CLIENT_IP}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP


@pytest.mark.unit
def test_parse_forwarded_header_ipv6_and_quoted_forms() -> None:
    chain = parse_forwarded_header(
        'for="[2001:db8::1]";proto=https, for="10.0.0.1"'
    )
    assert chain == ("2001:db8::1", "10.0.0.1")


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    result = resolve_admin_login_client_source(Request(scope), settings)
    assert result.source == "unknown"
    assert result.path == AdminClientSourcePath.MISSING_PEER


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_uses_default_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_cidrs="", legacy_trust=True)
    xff = f"{CLIENT_IP}, {RENDER_PROXY}"
    request = _request_with_client(
        RENDER_PROXY,
        headers=[(b"x-forwarded-for", xff.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_IP


@pytest.mark.unit
def test_invalid_forwarding_telemetry_has_no_raw_ip(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(
        DIRECT_PEER,
        headers=[(b"x-forwarded-for", f"{ATTACKER_SPOOF}, {CLIENT_IP}".encode())],
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    assert DIRECT_PEER not in caplog.text
    assert ATTACKER_SPOOF not in caplog.text
    assert any(
        getattr(record, "client_source_path", None) == "invalid_forwarding"
        for record in caplog.records
    )


@pytest.mark.integration
def test_uvicorn_proxy_middleware_does_not_poison_admin_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise ProxyHeadersMiddleware + resolver (deployment stack)."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    settings = get_settings()

    inner = FastAPI()

    @inner.get("/probe")
    def probe(request: Request) -> dict[str, str]:
        result = resolve_admin_login_client_source(request, settings)
        return {"source": result.source, "path": result.path.value}

    wrapped = ProxyHeadersMiddleware(inner, trusted_hosts=TRUSTED_CIDRS.split(","))
    client = TestClient(wrapped)

    xff = f"{ATTACKER_SPOOF}, {CLIENT_IP}, {RENDER_PROXY}"
    response = client.get(
        "/probe",
        headers={
            "X-Forwarded-For": xff,
            "X-Forwarded-Proto": "https",
        },
    )
    payload = response.json()
    assert payload["source"] == CLIENT_IP
    assert payload["path"] == AdminClientSourcePath.XFF_TRUSTED_CHAIN.value
