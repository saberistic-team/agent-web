"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    RESOLUTION_DIRECT_PEER,
    RESOLUTION_MALFORMED_FORWARDING,
    RESOLUTION_TRUSTED_CHAIN,
    RESOLUTION_UNTRUSTED_FORWARDING,
    SourceResolution,
    is_trusted_proxy_address,
    normalize_ip_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.trusted_proxy_defaults import (
    CLOUDFLARE_PROXY_CIDRS,
    PRODUCTION_CLOUDFLARE_PROXY_CIDRS,
    PRODUCTION_TRUSTED_PROXY_CIDRS,
    RENDER_TRUSTED_PROXY_CIDRS,
    UVICORN_FORWARDED_ALLOW_IPS,
    parse_trusted_proxy_cidrs,
)

RENDER_PROXY = "10.0.0.5"
CLOUDFLARE_EDGE = "104.16.0.1"
CLIENT_IPV4 = "203.0.113.77"
CLIENT_IPV6 = "2001:db8::9"
UNTRUSTED_PEER = "198.51.100.10"

TEST_TRUSTED_CIDRS = "10.0.0.0/8,104.16.0.0/13"
TEST_CLOUDFLARE_CIDRS = "104.16.0.0/13"


def _settings(
    *,
    trust_proxy: bool = True,
    trusted_cidrs: str = TEST_TRUSTED_CIDRS,
    cloudflare_cidrs: str = TEST_CLOUDFLARE_CIDRS,
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret-secret-secret-secret",
        admin_trust_proxy_headers=trust_proxy,
        admin_trusted_proxy_cidrs=parse_trusted_proxy_cidrs(trusted_cidrs),
        admin_cloudflare_proxy_cidrs=parse_trusted_proxy_cidrs(cloudflare_cidrs),
    )


def _request(
    peer: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list: list[tuple[bytes, bytes]] = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode("ascii"), value.encode("ascii")))
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address(" 203.0.113.1:443 ") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::9") == "2001:db8::9"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::9]:443") == "2001:db8::9"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings(trust_proxy=False)
    for header in ("203.0.113.99", "203.0.113.99, 10.0.0.1"):
        resolution = resolve_admin_login_client_source(
            _request(UNTRUSTED_PEER, headers={"X-Forwarded-For": header}),
            settings,
        )
        assert resolution == SourceResolution(
            source=UNTRUSTED_PEER,
            path=RESOLUTION_DIRECT_PEER,
        )


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings()
    xff = f"203.0.113.50, {CLIENT_IPV4}, {CLOUDFLARE_EDGE}"
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers={"X-Forwarded-For": xff}),
        settings,
    )
    assert resolution == SourceResolution(
        source=CLIENT_IPV4,
        path=RESOLUTION_TRUSTED_CHAIN,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings()
    xff = f"{CLIENT_IPV4}, {CLOUDFLARE_EDGE}"
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers={"X-Forwarded-For": xff}),
        settings,
    )
    assert resolution.source == CLIENT_IPV4
    assert resolution.path == RESOLUTION_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings()
    xff = f"{CLIENT_IPV4}, {UNTRUSTED_PEER}, {RENDER_PROXY}"
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers={"X-Forwarded-For": xff}),
        settings,
    )
    assert resolution == SourceResolution(
        source=UNTRUSTED_PEER,
        path=RESOLUTION_TRUSTED_CHAIN,
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request(
            UNTRUSTED_PEER,
            headers={
                "CF-Connecting-IP": CLIENT_IPV4,
                "X-Forwarded-For": CLIENT_IPV4,
            },
        ),
        settings,
    )
    assert resolution == SourceResolution(
        source=UNTRUSTED_PEER,
        path=RESOLUTION_UNTRUSTED_FORWARDING,
    )


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf_connecting_ip() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            headers={
                "X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_EDGE}",
                "Forwarded": f'for="203.0.113.1"',
                "CF-Connecting-IP": "203.0.113.1",
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_IPV4
    assert resolution.path == RESOLUTION_TRUSTED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            headers={
                "Forwarded": (
                    f'for="{CLIENT_IPV4}";proto=https, '
                    f'for="{CLOUDFLARE_EDGE}";proto=https'
                ),
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_IPV4
    assert resolution.path == RESOLUTION_TRUSTED_CHAIN


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_falls_back_to_peer() -> None:
    settings = _settings()
    overlong = ",".join(["203.0.113.1"] * 40)
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers={"X-Forwarded-For": overlong}),
        settings,
    )
    assert resolution == SourceResolution(
        source=RENDER_PROXY,
        path=RESOLUTION_MALFORMED_FORWARDING,
    )

    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers={"X-Forwarded-For": " , "}),
        settings,
    )
    assert resolution.path == RESOLUTION_MALFORMED_FORWARDING


@pytest.mark.unit
def test_ipv6_client_in_trusted_chain() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_PROXY,
            headers={"X-Forwarded-For": f"{CLIENT_IPV6}, {CLOUDFLARE_EDGE}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_IPV6
    assert resolution.path == RESOLUTION_TRUSTED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_verified_and_xff_absent() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request(
            CLOUDFLARE_EDGE,
            headers={"CF-Connecting-IP": CLIENT_IPV4},
        ),
        settings,
    )
    assert resolution.source == CLIENT_IPV4
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_client_ip_delegates_to_resolver() -> None:
    settings = _settings(trust_proxy=False)
    request = _request(UNTRUSTED_PEER, headers={"X-Forwarded-For": "203.0.113.99"})
    assert admin_auth.client_ip(request, settings) == UNTRUSTED_PEER


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    store = FakeRateLimitStore()
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    settings = get_settings()

    with shared_rate_limiter(store):
        for spoofed in (f"203.0.113.{i}" for i in range(5)):
            request = _request(
                RENDER_PROXY,
                headers={"X-Forwarded-For": f"{spoofed}, {CLIENT_IPV4}, {CLOUDFLARE_EDGE}"},
            )
            admission = admin_auth.try_admit_login_attempt(request, settings)
            assert admission.admitted

        blocked = admin_auth.try_admit_login_attempt(
            _request(
                RENDER_PROXY,
                headers={"X-Forwarded-For": f"203.0.113.99, {CLIENT_IPV4}, {CLOUDFLARE_EDGE}"},
            ),
            settings,
        )
        assert blocked.throttled

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_IPV4)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.unit
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    caplog.set_level(logging.INFO)
    request = _request(
        UNTRUSTED_PEER,
        headers={
            "X-Forwarded-For": f"{CLIENT_IPV4}, {CLOUDFLARE_EDGE}",
            "CF-Connecting-IP": CLIENT_IPV4,
        },
    )
    admin_auth.try_admit_login_attempt(request, settings)
    combined = caplog.text
    assert CLIENT_IPV4 not in combined
    assert CLOUDFLARE_EDGE not in combined
    assert "x-forwarded-for" not in combined.lower()

    key = admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)
    assert CLIENT_IPV4 not in key
    assert len(key) == 64


@pytest.mark.unit
def test_invalid_forwarding_emits_sampled_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    caplog.set_level(logging.WARNING)
    request = _request(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": CLIENT_IPV4},
    )
    resolve_admin_login_client_source(request, settings)
    assert "rejected forwarding headers" in caplog.text
    assert CLIENT_IPV4 not in caplog.text


@pytest.mark.unit
def test_production_defaults_are_consistent() -> None:
    assert "10.0.0.0/8" in PRODUCTION_TRUSTED_PROXY_CIDRS
    for cidr in CLOUDFLARE_PROXY_CIDRS:
        assert cidr in PRODUCTION_TRUSTED_PROXY_CIDRS
    assert PRODUCTION_CLOUDFLARE_PROXY_CIDRS == ",".join(CLOUDFLARE_PROXY_CIDRS)
    assert "10.0.0.0/8" in UVICORN_FORWARDED_ALLOW_IPS
    assert is_trusted_proxy_address(
        RENDER_PROXY,
        (
            __import__("app.admin_client_source", fromlist=["_trusted_networks"])
            ._trusted_networks(RENDER_TRUSTED_PROXY_CIDRS)
        ),
    )
