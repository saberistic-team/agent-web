"""Unit tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.client_source import (
    ClientSourceResolution,
    normalize_client_ip,
    reset_untrusted_forwarding_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings


def _settings(*, trusted_cidrs: str = "") -> Settings:
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
        admin_session_secret="secret-32-chars-minimum-length!!",
        admin_trusted_proxy_cidrs=tuple(
            part.strip()
            for part in trusted_cidrs.split(",")
            if part.strip()
        ),
    )


def _request(
    peer_host: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in (headers or {}).items()
        ],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer_host is not None:
        scope["client"] = (peer_host, 12345)
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_untrusted_forwarding_telemetry_for_tests()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("[2001:db8::1]:8080", "2001:db8::1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1:abc", None),
    ],
)
def test_normalize_client_ip(raw: str, expected: str | None) -> None:
    assert normalize_client_ip(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored() -> None:
    settings = _settings()
    for header_value in ("203.0.113.99", "203.0.113.99, 198.51.100.10"):
        resolution = resolve_admin_login_client_source(
            _request("198.51.100.10", {"X-Forwarded-For": header_value}),
            settings,
        )
        assert resolution == ClientSourceResolution(
            source="198.51.100.10",
            path="direct_peer",
            ignored_untrusted_forwarding=True,
        )


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_xff() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
        ),
        settings,
    )
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_uses_cf_connecting_ip() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "CF-Connecting-IP": "203.0.113.50",
                "X-Forwarded-For": "203.0.113.99, 198.51.100.10",
            },
        ),
        settings,
    )
    assert resolution == ClientSourceResolution(
        source="203.0.113.50",
        path="cf_connecting_ip",
    )


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "203.0.113.5",
            {
                "CF-Connecting-IP": "203.0.113.50",
                "X-Forwarded-For": "203.0.113.50, 10.0.0.1",
            },
        ),
        settings,
    )
    assert resolution.source == "203.0.113.5"
    assert resolution.path == "direct_peer"
    assert resolution.ignored_untrusted_forwarding is True


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_headers() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "198.51.100.10",
            {
                "CF-Connecting-IP": "203.0.113.50",
                "X-Forwarded-For": "203.0.113.50",
            },
        ),
        settings,
    )
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_header_precedence_cf_over_forwarded_and_xff() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "CF-Connecting-IP": "203.0.113.1",
                "Forwarded": 'for=203.0.113.2;proto=https',
                "X-Forwarded-For": "203.0.113.3, 10.0.0.1",
            },
        ),
        settings,
    )
    assert resolution.path == "cf_connecting_ip"
    assert resolution.source == "203.0.113.1"


@pytest.mark.unit
def test_header_precedence_forwarded_before_xff_without_cf() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {
                "Forwarded": 'for="203.0.113.2";proto=https',
                "X-Forwarded-For": "203.0.113.3, 10.0.0.1",
            },
        ),
        settings,
    )
    assert resolution.path == "forwarded"
    assert resolution.source == "203.0.113.2"


@pytest.mark.unit
def test_overlong_xff_chain_is_conservative() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    long_chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    resolution = resolve_admin_login_client_source(
        _request("10.0.0.1", {"X-Forwarded-For": long_chain}),
        settings,
    )
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == "10.0.0.1"


@pytest.mark.unit
def test_missing_peer_uses_unknown() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(_request(None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == "unknown"


@pytest.mark.unit
def test_invalid_peer_with_headers_uses_unknown() -> None:
    settings = _settings()
    resolution = resolve_admin_login_client_source(
        _request("testclient", {"X-Forwarded-For": "203.0.113.1"}),
        settings,
    )
    assert resolution.source == "unknown"
    assert resolution.path == "unknown"


@pytest.mark.unit
def test_untrusted_forwarding_logs_do_not_contain_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(
            _request("198.51.100.10", {"X-Forwarded-For": "203.0.113.99"}),
            settings,
        )
    assert "203.0.113.99" not in caplog.text
    assert "198.51.100.10" not in caplog.text


@pytest.mark.unit
def test_build_source_rate_limit_key_never_contains_raw_forwarding_data() -> None:
    source = resolve_admin_login_client_source(
        _request("198.51.100.10", {"X-Forwarded-For": "203.0.113.99"}),
        _settings(),
    ).source
    key = admin_auth.build_source_rate_limit_key(source)
    assert "203.0.113.99" not in key
    assert "198.51.100.10" not in key
    assert len(key) == 64


@pytest.mark.unit
def test_empty_xff_elements_and_whitespace_are_skipped() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request("10.0.0.1", {"X-Forwarded-For": " , , 203.0.113.5 , 10.0.0.1"}),
        settings,
    )
    assert resolution.source == "203.0.113.5"


@pytest.mark.unit
def test_duplicate_addresses_in_xff_chain() -> None:
    settings = _settings(trusted_cidrs="10.0.0.0/8")
    resolution = resolve_admin_login_client_source(
        _request(
            "10.0.0.1",
            {"X-Forwarded-For": "203.0.113.5, 203.0.113.5, 10.0.0.1"},
        ),
        settings,
    )
    assert resolution.source == "203.0.113.5"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key() -> None:
    settings = _settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request("198.51.100.10", {"X-Forwarded-For": f"203.0.113.{index}"}),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_get_settings_parses_trusted_proxy_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8, 172.16.0.0/12",
    )
    settings = get_settings()
    assert settings.admin_trusted_proxy_cidrs == ("10.0.0.0/8", "172.16.0.0/12")

