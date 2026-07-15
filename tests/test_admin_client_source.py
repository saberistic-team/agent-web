"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request

from app.admin_client_source import (
    PRODUCTION_TRUSTED_PROXY_CIDRS,
    RENDER_FORWARDED_ALLOW_IPS,
    client_ip,
    normalize_client_address,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "173.245.48.10"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
SPOOF = "198.51.100.99"
UNTRUSTED_PEER = "198.51.100.10"

TEST_TRUSTED_CIDRS = "10.0.0.0/8,173.245.48.0/20"


def _settings(
    *,
    trust: bool = True,
    trusted_cidrs: str = TEST_TRUSTED_CIDRS,
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="ops@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret-secret-secret-secret",
        admin_trust_proxy_headers=trust,
        admin_trusted_proxy_cidrs=trusted_cidrs,
    )


def _request(
    *,
    peer: str | None = RENDER_LB,
    headers: dict[str, str] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in (headers or {}).items()
        ],
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings(trust=False)
    for header in (
        SPOOF,
        f"{SPOOF}, {CLIENT_A}",
        f"{SPOOF}, {CLIENT_A}, {RENDER_LB}",
    ):
        request = _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": header})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == UNTRUSTED_PEER
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings()
    header = f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_EDGE}, {RENDER_LB}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": header})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings()
    header = f"{CLIENT_B}, {CLOUDFLARE_EDGE}, {RENDER_LB}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": header})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings()
    header = f"{CLIENT_A}, {RENDER_LB}"
    request = _request(peer=UNTRUSTED_PEER, headers={"X-Forwarded-For": header})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={"CF-Connecting-IP": SPOOF},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}",
            "Forwarded": f'for="{CLIENT_B}";proto=https',
            "CF-Connecting-IP": SPOOF,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={"Forwarded": f'for="{CLIENT_B}";proto=https, for={RENDER_LB}'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == "forwarded_trusted_chain"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("x" * 129, None),
    ],
)
def test_address_normalization(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_invalid_xff_elements_fail_closed_to_peer() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": f"not-an-ip, {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_empty_xff_elements_are_invalid() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": f"{CLIENT_A}, , {RENDER_LB}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_excessive_xff_chain_length_rejected() -> None:
    settings = _settings()
    long_chain = ", ".join([f"203.0.113.{index}" for index in range(40)])
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": long_chain})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_cf_connecting_ip_precedence_when_xff_has_only_trusted_hops() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={
            "CF-Connecting-IP": CLIENT_A,
            "X-Forwarded-For": f"{CLOUDFLARE_EDGE}, {RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_missing_peer_uses_unknown() -> None:
    settings = _settings(trust=False)
    request = _request(peer=None)
    assert client_ip(request, settings) == "unknown"


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}"},
    )
    with caplog.at_level(logging.DEBUG, logger="app.admin_client_source"):
        resolve_admin_login_client_source(request, settings)
    assert CLIENT_A not in caplog.text
    assert RENDER_LB not in caplog.text
    assert any(
        getattr(record, "client_source_path", None) == "xff_trusted_chain"
        for record in caplog.records
    )


@pytest.mark.unit
def test_invalid_forwarding_sampled_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    request = _request(
        peer=UNTRUSTED_PEER,
        headers={"X-Forwarded-For": SPOOF},
    )
    with caplog.at_level(logging.INFO, logger="app.admin_client_source"):
        for _ in range(3):
            resolve_admin_login_client_source(request, settings)
    assert SPOOF not in caplog.text
    assert UNTRUSTED_PEER not in caplog.text
    assert any(
        "untrusted forwarding header" in record.message
        for record in caplog.records
    )


@pytest.mark.unit
def test_production_cidr_constants_match_render_yaml() -> None:
    assert "10.0.0.0/8" in PRODUCTION_TRUSTED_PROXY_CIDRS
    assert "173.245.48.0/20" in PRODUCTION_TRUSTED_PROXY_CIDRS
    assert RENDER_FORWARDED_ALLOW_IPS.startswith("10.0.0.0/8")


@pytest.mark.unit
def test_get_settings_proxy_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    settings = get_settings()
    assert settings.admin_trust_proxy_headers is True
    assert settings.admin_trusted_proxy_cidrs == "10.0.0.0/8"
    assert settings.uvicorn_forwarded_allow_ips == "10.0.0.0/8"
