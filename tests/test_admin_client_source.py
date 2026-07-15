"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_address,
    parse_forward_chain,
    parse_forwarded_header,
    reset_untrusted_forwarding_telemetry_for_tests,
    resolve_admin_login_client_source,
    resolve_admin_login_client_source_detail,
)
from app.config import Settings, get_settings

TRUSTED_CIDRS = ("10.0.0.0/8",)
EDGE_CIDRS = ("198.51.100.0/24",)


def _settings(
    *,
    trusted: tuple[str, ...] = TRUSTED_CIDRS,
    edge: tuple[str, ...] = EDGE_CIDRS,
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
        admin_trusted_proxy_cidrs=trusted,
        admin_edge_proxy_cidrs=edge,
    )


def _request(
    *,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_untrusted_forwarding_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings(trusted=())
    for header in ("203.0.113.99", "203.0.113.99, 198.51.100.10"):
        request = _request(
            peer="198.51.100.10",
            headers={"X-Forwarded-For": header},
        )
        assert resolve_admin_login_client_source(request, settings) == "198.51.100.10"
        detail = resolve_admin_login_client_source_detail(request, settings)
        assert detail.path == "direct_peer"


@pytest.mark.unit
def test_uvicorn_proxy_rewrite_still_uses_trusted_chain() -> None:
    """Uvicorn may set request.client to the leftmost X-Forwarded-For hop."""
    settings = _settings()
    request = _request(
        peer="203.0.113.50",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == "203.0.113.50"
    assert detail.path == "trusted_xff_right"


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.77, 198.51.100.44",
        },
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.77"
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.path == "trusted_xff_right"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.50"


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary() -> None:
    settings = _settings()
    request = _request(
        peer="203.0.113.5",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    assert resolve_admin_login_client_source(request, settings) == "203.0.113.5"
    assert (
        resolve_admin_login_client_source_detail(request, settings).path == "direct_peer"
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.9",
        headers={
            "CF-Connecting-IP": "203.0.113.60",
            "X-Forwarded-For": "203.0.113.60",
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == "203.0.113.60"
    assert detail.path == "trusted_xff_right"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_edge_path_proven() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.88, 198.51.100.10",
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == "203.0.113.88"
    assert detail.path == "cf_connecting_ip_verified"


@pytest.mark.unit
def test_multiple_header_families_follow_documented_precedence() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.3",
        headers={
            "CF-Connecting-IP": "203.0.113.10",
            "X-Forwarded-For": "203.0.113.10, 198.51.100.10",
            "Forwarded": 'for=203.0.113.99;proto=https, for="[2001:db8::5]";proto=https',
        },
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == "203.0.113.10"
    assert detail.path == "cf_connecting_ip_verified"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings()
    request = _request(
        peer="10.0.0.4",
        headers={"Forwarded": "for=203.0.113.41;proto=https, for=10.0.0.4"},
    )
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail.source == "203.0.113.41"
    assert detail.path == "trusted_forwarded_header"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1%eth0", None),
    ],
)
def test_normalize_client_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_parse_forward_chain_rejects_invalid_and_overlong_values() -> None:
    assert parse_forward_chain("203.0.113.1, , 203.0.113.2") == (
        "203.0.113.1",
        "203.0.113.2",
    )
    assert parse_forward_chain("bad, 203.0.113.2") == ()
    assert parse_forward_chain(",203.0.113.3") == ("203.0.113.3",)
    overlong = ",".join(f"203.0.113.{index}" for index in range(40))
    assert parse_forward_chain(overlong) == ()


@pytest.mark.unit
def test_parse_forwarded_header_supports_ipv6_and_ports() -> None:
    assert parse_forwarded_header('for="[2001:db8::9]:4711";proto=https') == (
        "2001:db8::9",
    )
    assert parse_forwarded_header("for=203.0.113.7;proto=https") == ("203.0.113.7",)


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    settings = _settings(trusted=())
    request = _request(peer=None)
    detail = resolve_admin_login_client_source_detail(request, settings)
    assert detail == ClientSourceResolution(source="unknown", path="missing_peer")


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trusted=())
    request = _request(
        peer="198.51.100.20",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source_detail(request, settings)
    messages = " ".join(record.message for record in caplog.records)
    assert "203.0.113.99" not in messages
    assert "198.51.100.20" not in messages
    assert any(
        getattr(record, "resolution_path", None) == "untrusted_peer_forwarding_ignored"
        for record in caplog.records
    )
