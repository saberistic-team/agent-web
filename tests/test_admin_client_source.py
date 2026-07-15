"""Tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourcePath,
    log_client_source_telemetry,
    normalize_client_ip,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED)
    return get_settings()


def _request(
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    if headers:
        scope["headers"] = [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in headers.items()
        ]
    return Request(scope)


def _resolve(
    settings: Settings,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> tuple[str, ClientSourcePath]:
    resolution = resolve_admin_login_client_source(_request(peer, headers), settings)
    return resolution.source, resolution.path


@pytest.mark.unit
def test_normalize_ipv4_and_ipv6() -> None:
    assert normalize_client_ip("203.0.113.1") == "203.0.113.1"
    assert normalize_client_ip("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_ip("2001:db8::1") == "2001:db8::1"
    assert normalize_client_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_ip("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_ip("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_ip("") is None
    assert normalize_client_ip("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "198.51.100.10",
        {"X-Forwarded-For": "203.0.113.99"},
    )
    assert source == "198.51.100.10"
    assert path is ClientSourcePath.UNTRUSTED_PEER

    source, path = _resolve(
        trusted_proxy_env,
        "198.51.100.10",
        {"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    assert source == "198.51.100.10"
    assert path is ClientSourcePath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.2",
        {"X-Forwarded-For": "203.0.113.99, 203.0.113.50, 10.0.0.2"},
    )
    assert source == "203.0.113.50"
    assert path is ClientSourcePath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.5",
        {"X-Forwarded-For": "203.0.113.77, 10.0.0.5"},
    )
    assert source == "203.0.113.77"
    assert path is ClientSourcePath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "198.51.100.20",
        {"X-Forwarded-For": "203.0.113.77, 10.0.0.5, 198.51.100.20"},
    )
    assert source == "198.51.100.20"
    assert path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_headers(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.8",
        {
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.88, 10.0.0.8",
        },
    )
    assert source == "203.0.113.88"
    assert path is ClientSourcePath.TRUSTED_X_FORWARDED_FOR

    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.8",
        {"CF-Connecting-IP": "203.0.113.88"},
    )
    assert source == "10.0.0.8"
    assert path is ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_cf_connecting_ip_used_with_cf_ray(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.9",
        {
            "CF-Connecting-IP": "203.0.113.90",
            "CF-Ray": "abc123",
        },
    )
    assert source == "203.0.113.90"
    assert path is ClientSourcePath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.3",
        {
            "X-Forwarded-For": "203.0.113.10, 10.0.0.3",
            "Forwarded": 'for=203.0.113.11;proto=https, for=10.0.0.3;proto=https',
            "CF-Connecting-IP": "203.0.113.12",
            "CF-Ray": "ray-1",
        },
    )
    assert source == "203.0.113.10"
    assert path is ClientSourcePath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.4",
        {
            "Forwarded": 'for=203.0.113.20;proto=https, for=10.0.0.4;proto=https',
        },
    )
    assert source == "203.0.113.20"
    assert path is ClientSourcePath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_address_format_edge_cases(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.6",
        {"X-Forwarded-For": " , 203.0.113.30"},
    )
    assert source == "unknown"
    assert path is ClientSourcePath.MALFORMED

    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.6",
        {"X-Forwarded-For": "not-valid, 10.0.0.6"},
    )
    assert source == "unknown"
    assert path is ClientSourcePath.MALFORMED

    long_chain = ", ".join(f"203.0.113.{index}" for index in range(70))
    source, path = _resolve(
        trusted_proxy_env,
        "10.0.0.6",
        {"X-Forwarded-For": f"{long_chain}, 10.0.0.6"},
    )
    assert source == "unknown"
    assert path is ClientSourcePath.MALFORMED


@pytest.mark.unit
def test_proxy_trust_disabled_uses_direct_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    source, path = _resolve(
        settings,
        "198.51.100.30",
        {"X-Forwarded-For": "203.0.113.99"},
    )
    assert source == "198.51.100.30"
    assert path is ClientSourcePath.PROXY_TRUST_DISABLED


@pytest.mark.unit
def test_missing_peer_is_unknown(
    trusted_proxy_env: Settings,
) -> None:
    source, path = _resolve(
        trusted_proxy_env,
        None,
        {"X-Forwarded-For": "203.0.113.1"},
    )
    assert source == "unknown"
    assert path is ClientSourcePath.MISSING_PEER


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(
    trusted_proxy_env: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    resolution = resolve_admin_login_client_source(
        _request("198.51.100.10", {"X-Forwarded-For": "203.0.113.99"}),
        trusted_proxy_env,
    )
    log_client_source_telemetry(resolution)
    assert any(
        record.__dict__.get("admin_login_source_path") == ClientSourcePath.UNTRUSTED_PEER.value
        for record in caplog.records
    )
    assert "203.0.113.99" not in caplog.text
    assert "198.51.100.10" not in caplog.text
