"""Unit tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourcePath,
    normalize_client_address,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
    resolve_from_chain_right_to_left,
    split_forwarding_chain,
)
from app.config import get_settings
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    shared_rate_limiter,
)

# Test stand-ins for production proxy roles (RFC 5737 documentation ranges).
RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "203.0.113.200"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
UNTRUSTED_PEER = "198.51.100.10"

TEST_TRUSTED_PROXY_IPS = "127.0.0.1,::1,10.0.0.0/8"
TEST_CLOUDFLARE_CIDRS = "203.0.113.200/32"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def _proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", TEST_TRUSTED_PROXY_IPS)
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_HEADERS", "true")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", TEST_CLOUDFLARE_CIDRS)
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": CLIENT_A},
    )
    assert resolve_admin_login_client_source(request, settings).source == UNTRUSTED_PEER

    request = _request_with_client(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {CLIENT_B}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == ClientSourcePath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"203.0.113.1, {CLIENT_A}, {CLOUDFLARE_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == ClientSourcePath.X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_B}, {CLOUDFLARE_EDGE}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary() -> None:
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == ClientSourcePath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT_A,
            "X-Forwarded-For": CLIENT_B,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path == ClientSourcePath.X_FORWARDED_FOR


@pytest.mark.unit
def test_verified_cf_connecting_ip_precedence_over_conflicting_headers() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT_A,
            "X-Forwarded-For": f"{CLIENT_B}, {CLOUDFLARE_EDGE}",
            "Forwarded": f'for="{CLIENT_B}";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == ClientSourcePath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_fallback_when_xff_missing() -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"Forwarded": f'for="{CLIENT_A}";proto=https, for="{RENDER_PROXY}"'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == ClientSourcePath.FORWARDED_HEADER


@pytest.mark.unit
def test_address_format_edge_cases_are_deterministic() -> None:
    from app.admin_client_source import parse_trusted_proxy_networks

    trusted = parse_trusted_proxy_networks("10.0.0.0/8")
    assert (
        resolve_from_chain_right_to_left(
            [" 203.0.113.1 ", "10.0.0.1"],
            trusted,
        )
        == "203.0.113.1"
    )
    assert (
        resolve_from_chain_right_to_left(
            ["203.0.113.1:8080", "10.0.0.1"],
            trusted,
        )
        == "203.0.113.1"
    )
    assert split_forwarding_chain("a," + "b," * 2000) == []
    assert resolve_from_chain_right_to_left(["not-valid", "10.0.0.1"], trusted) is None


@pytest.mark.unit
def test_malformed_or_empty_chain_maps_to_unknown() -> None:
    settings = get_settings()
    request = _request_with_client(RENDER_PROXY, headers={"X-Forwarded-For": "10.0.0.1"})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == ClientSourcePath.MALFORMED


@pytest.mark.unit
def test_proxy_trust_disabled_uses_direct_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": CLIENT_A},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == ClientSourcePath.DIRECT_PEER


@pytest.mark.unit
def test_limiter_rows_store_only_digests_not_raw_sources(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import _login

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient,10.0.0.0/8")
    with shared_rate_limiter(rate_limit_store):
        response = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": f"203.0.113.1, {CLIENT_A}, {CLOUDFLARE_EDGE}"},
        )
        assert response.status_code == 401

    for key, row in rate_limit_store.rows.items():
        assert len(key) == 64
        assert CLIENT_A not in key
        assert "x-forwarded-for" not in str(row).lower()
        assert "cf-connecting-ip" not in str(row).lower()


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        UNTRUSTED_PEER,
        headers={"X-Forwarded-For": CLIENT_A},
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    logged = " ".join(record.message for record in caplog.records)
    assert CLIENT_A not in logged
    assert UNTRUSTED_PEER not in logged
    assert any(
        getattr(record, "admin_client_source_path", None)
        == ClientSourcePath.UNTRUSTED_PEER.value
        for record in caplog.records
    )
