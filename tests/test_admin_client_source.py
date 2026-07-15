"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourceResolutionPath,
    normalize_client_address,
    parse_forwarded_header,
    parse_trusted_proxy_networks,
    reset_invalid_forwarding_telemetry_for_tests,
    resolve_admin_login_client_source,
    resolve_client_from_forwarding_chain,
    split_forwarding_chain,
)
from app.config import Settings, get_settings

TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128"
TRUSTED_NETWORKS = parse_trusted_proxy_networks(TRUSTED_CIDRS)


def _settings(**overrides: object) -> Settings:
    base = get_settings()
    data = base.__dict__.copy()
    data.update(overrides)
    return Settings(**data)


def _request(
    *,
    peer: str | None = "203.0.113.10",
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:0db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    for header in ("203.0.113.99", "203.0.113.99, 198.51.100.20"):
        request = _request(
            peer="203.0.113.10",
            headers={"X-Forwarded-For": header},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "203.0.113.10"
        assert resolution.path is ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.99, 203.0.113.10, 10.0.0.2",
            "CF-Connecting-IP": "203.0.113.10",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.50, 10.0.0.5",
            "CF-Connecting-IP": "203.0.113.50",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_ignores_headers() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="198.51.100.99",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.99"
    assert resolution.path is ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="203.0.113.10",
        headers={"CF-Connecting-IP": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_trusted_peer_cf_header_without_chain_fails_closed() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={"CF-Connecting-IP": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.AMBIGUOUS_FORWARDING


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.10, 10.0.0.2",
            "Forwarded": 'for="203.0.113.20";proto=https',
            "CF-Connecting-IP": "203.0.113.10",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_xff_without_cf_header_fails_closed() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.AMBIGUOUS_FORWARDING


@pytest.mark.unit
def test_trusted_xff_cf_mismatch_fails_closed() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.10, 10.0.0.2",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={"Forwarded": 'for="203.0.113.44";proto=https, for=10.0.0.2'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path is ClientSourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_address_format_edge_cases() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": " , , 203.0.113.55 , 10.0.0.2 ",
            "CF-Connecting-IP": "203.0.113.55",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.55"

    invalid = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.2"},
    )
    invalid_resolution = resolve_admin_login_client_source(invalid, settings)
    assert invalid_resolution.source == "unknown"
    assert invalid_resolution.path is ClientSourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_overlong_chain_fails_closed() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    chain = ", ".join(f"203.0.113.{index}" for index in range(1, 40))
    request = _request(peer="10.0.0.2", headers={"X-Forwarded-For": f"{chain}, 10.0.0.2"})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.OVERLONG_CHAIN


@pytest.mark.unit
def test_missing_peer_uses_unknown() -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    resolution = resolve_admin_login_client_source(_request(peer=None), settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_default_cidrs() -> None:
    settings = _settings(admin_trust_proxy_headers=True, admin_trusted_proxy_cidrs="")
    request = _request(
        peer="10.0.0.1",
        headers={
            "X-Forwarded-For": "203.0.113.60, 10.0.0.1",
            "CF-Connecting-IP": "203.0.113.60",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"


@pytest.mark.unit
def test_split_forwarding_chain_and_forwarded_parser() -> None:
    assert split_forwarding_chain(" a , b , ") == ["a", "b"]
    assert parse_forwarded_header('for="203.0.113.1";proto=https, for=10.0.0.1') == [
        "203.0.113.1",
        "10.0.0.1",
    ]


@pytest.mark.unit
def test_resolve_client_from_forwarding_chain_all_trusted_returns_none() -> None:
    chain = ["10.0.0.1", "10.0.0.2"]
    assert resolve_client_from_forwarding_chain(chain, TRUSTED_NETWORKS) is None


@pytest.mark.unit
def test_invalid_forwarding_telemetry_is_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    reset_invalid_forwarding_telemetry_for_tests()
    with caplog.at_level(logging.WARNING, logger="app.admin_client_source"):
        for _ in range(3):
            resolve_admin_login_client_source(
                _request(peer="10.0.0.2", headers={"X-Forwarded-For": "bad-ip, 10.0.0.2"}),
                settings,
            )
    warnings = [
        record
        for record in caplog.records
        if record.message == "Admin login client source rejected forwarding headers"
    ]
    assert len(warnings) == 1
    assert "203.0.113" not in warnings[0].getMessage()
    assert warnings[0].client_source_resolution_path == "invalid_forwarding"


@pytest.mark.unit
def test_resolution_does_not_log_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(admin_trusted_proxy_cidrs=TRUSTED_CIDRS)
    caplog.set_level(logging.DEBUG)
    resolve_admin_login_client_source(
        _request(
            peer="10.0.0.2",
            headers={
                "X-Forwarded-For": "203.0.113.77, 10.0.0.2",
                "CF-Connecting-IP": "203.0.113.77",
            },
        ),
        settings,
    )
    for record in caplog.records:
        assert "203.0.113.77" not in record.getMessage()
        assert "x-forwarded-for" not in record.getMessage().lower()


@pytest.mark.unit
def test_mock_limiter_key_uses_digest_not_raw_ip() -> None:
    from app import admin_auth

    key = admin_auth.build_source_rate_limit_key("203.0.113.77")
    assert "203.0.113.77" not in key
    assert len(key) == 64
