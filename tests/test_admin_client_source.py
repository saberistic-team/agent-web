"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    SourceResolutionPath,
    client_from_xff_chain,
    normalize_client_address,
    parse_trusted_networks,
    parse_x_forwarded_for_chain,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings


def _settings(
    *,
    trusted_cidrs: tuple[str, ...] = (),
    trust_cloudflare: bool = False,
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
        admin_trusted_proxy_cidrs=trusted_cidrs,
        admin_trust_cloudflare_proxy=trust_cloudflare,
    )


def _request(
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
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
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1:bad", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_parse_x_forwarded_for_chain_rejects_overlong() -> None:
    assert parse_x_forwarded_for_chain("203.0.113.1, " + "1.1.1.1, " * 30) is None


@pytest.mark.unit
def test_parse_x_forwarded_for_chain_skips_empty_elements() -> None:
    assert parse_x_forwarded_for_chain("203.0.113.1,, 198.51.100.2") == [
        "203.0.113.1",
        "198.51.100.2",
    ]


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored_without_trusted_peer() -> None:
    settings = _settings()
    for header in ("203.0.113.99", "203.0.113.99, 198.51.100.10"):
        result = resolve_admin_login_client_source(
            _request("198.51.100.10", {"X-Forwarded-For": header}),
            settings,
        )
        assert result.source == "198.51.100.10"
        assert result.path is SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",), trust_cloudflare=True)
    result = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {"X-Forwarded-For": "203.0.113.99, 198.51.100.55, 104.16.0.1"},
        ),
        settings,
    )
    assert result.source == "198.51.100.55"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(
        _request("10.0.0.1", {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"}),
        settings,
    )
    assert result.source == "203.0.113.50"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(
        _request(
            "198.51.100.20",
            {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
        ),
        settings,
    )
    assert result.source == "198.51.100.20"
    assert result.path is SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",), trust_cloudflare=True)
    result = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "X-Forwarded-For": "203.0.113.77",
                "CF-Connecting-IP": "203.0.113.88",
            },
        ),
        settings,
    )
    assert result.source == "203.0.113.77"
    assert result.path is SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE


@pytest.mark.unit
def test_cf_connecting_ip_used_when_consistent_with_xff() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",), trust_cloudflare=True)
    result = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "X-Forwarded-For": "203.0.113.77, 104.16.0.1",
                "CF-Connecting-IP": "203.0.113.77",
            },
        ),
        settings,
    )
    assert result.source == "203.0.113.77"
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP_CONSISTENT


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {"Forwarded": 'for="203.0.113.60";proto=https'},
        ),
        settings,
    )
    assert result.source == "203.0.113.60"
    assert result.path is SourceResolutionPath.FORWARDED


@pytest.mark.unit
def test_conflicting_forwarded_and_xff_falls_back_to_peer() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "X-Forwarded-For": "203.0.113.1",
                "Forwarded": 'for="203.0.113.2";proto=https',
            },
        ),
        settings,
    )
    assert result.source == "10.0.0.1"
    assert result.path is SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE


@pytest.mark.unit
def test_malformed_xff_falls_back_to_peer() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(
        _request("10.0.0.1", {"X-Forwarded-For": "not-an-ip"}),
        settings,
    )
    assert result.source == "10.0.0.1"
    assert result.path is SourceResolutionPath.MALFORMED_FORWARDING_CONSERVATIVE


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    result = resolve_admin_login_client_source(_request(None), settings)
    assert result.source == "unknown"
    assert result.path is SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_client_from_xff_chain_all_trusted_uses_leftmost() -> None:
    trusted = parse_trusted_networks(("10.0.0.0/8",))
    assert client_from_xff_chain(["203.0.113.1", "10.0.0.2"], trusted) == "203.0.113.1"


@pytest.mark.unit
def test_telemetry_does_not_log_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(
            _request("10.0.0.1", {"X-Forwarded-For": "203.0.113.50"}),
            settings,
        )
    joined = " ".join(record.getMessage() for record in caplog.records)
    extra_blob = " ".join(str(record.__dict__) for record in caplog.records)
    assert "203.0.113.50" not in joined
    assert "203.0.113.50" not in extra_blob
    assert any(
        record.__dict__.get("admin_login_source_resolution_path")
        == SourceResolutionPath.X_FORWARDED_FOR.value
        for record in caplog.records
    )
