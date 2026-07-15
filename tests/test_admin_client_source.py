"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    TrustedProxyBoundary,
    normalize_client_address,
    parse_forwarded_header,
    parse_x_forwarded_for,
    resolve_admin_login_client_source,
    resolve_client_from_forwarding_chain,
)
from app.config import get_settings

RENDER_TRUSTED = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        (" 2001:db8::1 ", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ('"203.0.113.9"', "203.0.113.9"),
    ],
)
def test_normalize_client_address_formats(raw: str, expected: str) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["", "not-an-ip", "999.999.1.1", "::::", "x" * 3000],
)
def test_normalize_client_address_rejects_invalid(raw: str) -> None:
    assert normalize_client_address(raw) is None


@pytest.mark.unit
def test_parse_x_forwarded_for_rejects_overlong_chain() -> None:
    chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    assert parse_x_forwarded_for(chain) is None


@pytest.mark.unit
def test_parse_x_forwarded_for_rejects_empty_elements() -> None:
    assert parse_x_forwarded_for("203.0.113.1,,10.0.0.2") is None


@pytest.mark.unit
def test_parse_forwarded_header_extracts_for_values() -> None:
    header = (
        'for=203.0.113.5;proto=https, for="[2001:db8::2]";proto=https, for=_hidden'
    )
    assert parse_forwarded_header(header) == ["203.0.113.5", "2001:db8::2"]


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99, 10.0.0.5, 172.16.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.50"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.50"
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "198.51.100.10",
        headers={"CF-Connecting-IP": "203.0.113.77"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_prefers_xff_over_forwarded_and_cf(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.50",
            "Forwarded": 'for=203.0.113.88;proto=https',
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"Forwarded": 'for="203.0.113.60";proto=https, for=10.0.0.5'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path is SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_proxy_trust_disabled_ignores_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_malformed_xff_with_extra_hops_still_rejected(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "bad-ip, 198.51.100.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path is SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_resolve_client_from_forwarding_chain_right_to_left() -> None:
    boundary = TrustedProxyBoundary("10.0.0.0/8")
    client = resolve_client_from_forwarding_chain(
        ["203.0.113.50", "10.0.0.5"],
        immediate_peer="10.0.0.5",
        trusted_boundary=boundary,
    )
    assert client == "203.0.113.50"


@pytest.mark.unit
def test_untrusted_forwarding_attempt_emits_sampled_telemetry(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    with caplog.at_level(logging.INFO):
        for _ in range(8):
            request = _request_with_client(
                "198.51.100.10",
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            resolve_admin_login_client_source(request, settings)

    messages = [
        record.message
        for record in caplog.records
        if record.message == "Admin login forwarding headers ignored for untrusted peer"
    ]
    assert 1 <= len(messages) <= 5
    assert all(
        getattr(record, "source_resolution_path", None) == "invalid_forwarding"
        for record in caplog.records
        if record.message == "Admin login forwarding headers ignored for untrusted peer"
    )


@pytest.mark.unit
def test_limiter_keys_never_store_raw_addresses() -> None:
    source = "203.0.113.50"
    key = admin_auth.build_source_rate_limit_key(source)
    assert source not in key
    assert len(key) == 64


@pytest.mark.unit
def test_admission_logs_source_resolution_path_without_raw_ip(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED)
    settings = get_settings()
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    store = FakeRateLimitStore()
    with shared_rate_limiter(store), caplog.at_level(logging.INFO):
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    admitted = [
        record
        for record in caplog.records
        if record.message == "Admin login attempt admitted"
    ]
    assert admitted
    assert admitted[-1].source_resolution_path == "trusted_xff"
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "203.0.113.50" not in joined
    assert "x-forwarded-for" not in joined.lower()


@pytest.mark.unit
@pytest.mark.integration
def test_production_asgi_peer_with_xff_matches_deployment_boundary(
    trusted_proxy_env: None,
) -> None:
    """Exercise resolver with raw ASGI peer (Uvicorn --proxy-headers disabled)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    captured: dict[str, str] = {}

    async def endpoint(request: Request) -> JSONResponse:
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        captured["source"] = resolution.source
        captured["path"] = resolution.path.value
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/probe", endpoint)])
    probe_client = TestClient(app)

    trusted_peer = probe_client.get(
        "/probe",
        headers={
            "X-Forwarded-For": "203.0.113.99, 198.51.100.50",
        },
    )
    assert trusted_peer.status_code == 200
    assert captured["source"] == "testclient"
    assert captured["path"] == "direct_peer"

    trusted_render_peer = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.50"},
    )
    resolution = resolve_admin_login_client_source(trusted_render_peer, get_settings())
    assert resolution.source == "198.51.100.50"
    assert resolution.path is SourceResolutionPath.TRUSTED_XFF

    single_hop = resolve_admin_login_client_source(
        _request_with_client("10.0.0.5", headers={"X-Forwarded-For": "203.0.113.99"}),
        get_settings(),
    )
    assert single_hop.source == "203.0.113.99"
    assert single_hop.path is SourceResolutionPath.TRUSTED_XFF

    rotating_with_append = {
        resolve_admin_login_client_source(
            _request_with_client(
                "10.0.0.5",
                headers={"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.50"},
            ),
            get_settings(),
        ).source
        for i in range(5)
    }
    assert rotating_with_append == {"198.51.100.50"}
