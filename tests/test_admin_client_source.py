"""Tests for verified-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    AMBIGUOUS_FORWARDING_SENTINEL,
    ClientSourcePath,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from app.config import Settings

RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "172.64.0.1"
CLIENT_IP = "203.0.113.50"
SPOOFED_IP = "203.0.113.99"
ATTACKER_IP = "198.51.100.10"
DIRECT_PEER = "198.51.100.10"


def _settings(*, trust_proxy: bool = False, extra_cidrs: str = "", rate_limit: int = 5) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_trust_proxy_headers=trust_proxy,
        admin_trusted_proxy_cidrs=extra_cidrs,
        admin_login_rate_limit=rate_limit,
    )


def _request(
    *,
    peer: str | None = "testclient",
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": None if peer is None else (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.mark.unit
def test_normalize_client_address_handles_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("x" * 300) is None


@pytest.mark.unit
def test_direct_spoof_without_trust_ignores_forwarded_headers() -> None:
    settings = _settings(trust_proxy=False)
    for header_value in (
        SPOOFED_IP,
        f"{SPOOFED_IP}, {ATTACKER_IP}",
    ):
        request = _request(
            peer=DIRECT_PEER,
            headers={
                "X-Forwarded-For": header_value,
                "CF-Connecting-IP": SPOOFED_IP,
                "Forwarded": f'for={SPOOFED_IP};proto=https',
            },
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.address == DIRECT_PEER
        assert resolution.path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{SPOOFED_IP}, {ATTACKER_IP}, {CLOUDFLARE_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == ATTACKER_IP
    assert resolution.path is ClientSourcePath.FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_IP
    assert resolution.path is ClientSourcePath.FORWARDED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=DIRECT_PEER,
        headers={"X-Forwarded-For": f"{CLIENT_IP}, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == DIRECT_PEER
    assert resolution.path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_cloudflare_headers() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "CF-Connecting-IP": SPOOFED_IP,
            "CF-RAY": "fake-ray",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == AMBIGUOUS_FORWARDING_SENTINEL
    assert resolution.path is ClientSourcePath.AMBIGUOUS_FORWARDING


@pytest.mark.unit
def test_single_hop_forwarded_is_ambiguous_on_trusted_peer() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": SPOOFED_IP},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == AMBIGUOUS_FORWARDING_SENTINEL
    assert resolution.path is ClientSourcePath.AMBIGUOUS_FORWARDING


@pytest.mark.unit
def test_header_precedence_prefers_x_forwarded_for_over_forwarded() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{CLIENT_IP}, {RENDER_LB}",
            "Forwarded": f'for={SPOOFED_IP};proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_IP
    assert resolution.path is ClientSourcePath.FORWARDED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_x_forwarded_for_missing() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "Forwarded": f'for={CLIENT_IP};proto=https, for={RENDER_LB}',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_IP
    assert resolution.path is ClientSourcePath.FORWARDED_HEADER


@pytest.mark.unit
def test_cf_connecting_ip_used_when_chain_has_cloudflare_hop_and_is_ambiguous() -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_LB}",
            "CF-Connecting-IP": CLIENT_IP,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == CLIENT_IP
    assert resolution.path is ClientSourcePath.FORWARDED_CHAIN


@pytest.mark.unit
def test_overlong_and_invalid_forwarding_data_are_conservative() -> None:
    settings = _settings(trust_proxy=True)
    overlong = ", ".join([f"203.0.113.{index}" for index in range(40)])
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == RENDER_LB
    assert resolution.path is ClientSourcePath.DIRECT_PEER

    invalid = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
    )
    invalid_resolution = resolve_admin_login_client_source(invalid, settings)
    assert invalid_resolution.address == RENDER_LB
    assert invalid_resolution.path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_missing_peer_uses_unknown() -> None:
    settings = _settings(trust_proxy=False)
    resolution = resolve_admin_login_client_source(_request(peer=None), settings)
    assert resolution.address == "unknown"
    assert resolution.path is ClientSourcePath.UNKNOWN_PEER


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trust_proxy=True)
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": SPOOFED_IP},
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    combined = " ".join(record.message for record in caplog.records)
    assert SPOOFED_IP not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert any(
        getattr(record, "client_source_path", None) == ClientSourcePath.AMBIGUOUS_FORWARDING.value
        for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    settings = _settings(trust_proxy=True, rate_limit=2)

    with shared_rate_limiter(store):
        for index in range(5):
            request = _request(
                peer=RENDER_LB,
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            admission = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username="ghost",
            )
            if index < 2:
                assert admission.admitted
            else:
                assert admission.throttled

    assert len(store.rows) == 1
    assert (
        admin_auth.build_source_rate_limit_key(AMBIGUOUS_FORWARDING_SENTINEL) in store.rows
    )


@pytest.mark.unit
@pytest.mark.integration
def test_proxy_headers_middleware_matches_deployment_resolution() -> None:
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    settings = _settings(trust_proxy=True)
    render_chain = f"{CLIENT_IP}, {RENDER_LB}"
    render_trusted_hosts = _TrustedHosts(
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
    )
    uvicorn_host, _uvicorn_port = render_trusted_hosts.get_trusted_client_address(
        render_chain
    )
    assert uvicorn_host == CLIENT_IP

    cloudflare_chain = f"{CLIENT_IP}, {CLOUDFLARE_EDGE}, {RENDER_LB}"
    resolution = resolve_admin_login_client_source(
        _request(peer=RENDER_LB, headers={"X-Forwarded-For": cloudflare_chain}),
        settings,
    )
    assert resolution.address == CLIENT_IP
    assert resolution.path is ClientSourcePath.FORWARDED_CHAIN


@pytest.mark.unit
def test_health_reports_client_source_trust_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    assert TestClient(app).get("/health").json()["admin_client_source_trust"] == (
        "direct-peer-only"
    )

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    assert TestClient(app).get("/health").json()["admin_client_source_trust"] == (
        "verified-proxy-hops"
    )
