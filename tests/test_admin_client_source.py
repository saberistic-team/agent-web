"""Tests for verified admin login client-source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    client_ip,
    normalize_client_ip,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
CLOUDFLARE_EDGE = "172.64.0.0/13"


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": None if peer is None else (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "x")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_IPS", raising=False)
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = get_settings()
    for header in (
        "203.0.113.99",
        "203.0.113.1, 203.0.113.2, 203.0.113.3",
    ):
        request = _request(
            peer="198.51.100.10",
            headers={"X-Forwarded-For": header},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.99, 198.51.100.10",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    request = _request(
        peer="10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.50, 10.0.0.5",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    settings = get_settings()
    request = _request(
        peer="203.0.113.5",
        headers={"X-Forwarded-For": "198.51.100.77, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.5"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_IPS", CLOUDFLARE_EDGE)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "CF-Connecting-IP": "203.0.113.77",
            "X-Forwarded-For": "203.0.113.99, 198.51.100.10",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_trusted_edge_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_IPS", CLOUDFLARE_EDGE)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "CF-Connecting-IP": "203.0.113.44",
            "X-Forwarded-For": "203.0.113.99, 172.64.1.1",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_prefers_cf_when_edge_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_IPS", CLOUDFLARE_EDGE)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "CF-Connecting-IP": "203.0.113.10",
            "X-Forwarded-For": "198.51.100.20, 172.64.2.2",
            "Forwarded": 'for="198.51.100.99";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path == "cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_uses_xff_before_forwarded_without_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={
            "X-Forwarded-For": "203.0.113.20, 10.0.0.2",
            "Forwarded": 'for="198.51.100.99";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.20"
    assert resolution.path == "xff_trusted_chain"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1:notaport", None),
    ],
)
def test_normalize_client_ip_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_ip(raw) == expected


@pytest.mark.unit
def test_overlong_and_empty_chain_elements_are_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    long_chain = ", ".join(f"203.0.113.{index}" for index in range(12))
    overlong = _request(peer="10.0.0.2", headers={"X-Forwarded-For": long_chain})
    overlong_resolution = resolve_admin_login_client_source(overlong, settings)
    assert overlong_resolution.path == "invalid_forwarding"
    assert overlong_resolution.source == "10.0.0.2"

    empty_element = _request(peer="10.0.0.2", headers={"X-Forwarded-For": "203.0.113.1,,10.0.0.2"})
    empty_resolution = resolve_admin_login_client_source(empty_element, settings)
    assert empty_resolution.path == "invalid_forwarding"
    assert empty_resolution.source == "10.0.0.2"


@pytest.mark.unit
def test_missing_peer_maps_to_unknown() -> None:
    settings = get_settings()
    resolution = resolve_admin_login_client_source(_request(peer=None), settings)
    assert resolution.source == "unknown"
    assert resolution.path == "missing_peer"


@pytest.mark.unit
def test_invalid_forwarding_emits_sampled_telemetry_without_raw_ips(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    caplog.set_level(logging.WARNING)
    request = _request(peer="10.0.0.2", headers={"X-Forwarded-For": "a,,b"})
    resolve_admin_login_client_source(request, settings)
    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warning_records
    message = warning_records[-1].getMessage()
    assert "invalid" in message.lower() or "untrusted" in message.lower()
    assert "203.0.113" not in message
    assert warning_records[-1].__dict__.get("invalid_forwarding_sampled") is True


@pytest.mark.unit
def test_client_ip_wrapper_matches_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()
    request = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "203.0.113.55, 10.0.0.2"},
    )
    assert client_ip(request, settings) == "203.0.113.55"
