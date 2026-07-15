"""Unit tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging

import pytest
import uvicorn
from fastapi import FastAPI, Request

from app.admin_client_source import (
    PRODUCTION_TRUSTED_PROXY_IPS,
    ClientSourceResolution,
    normalize_client_source,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

pytestmark = pytest.mark.unit


def _request_with_client(host: str, headers: dict[str, str] | None = None) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.fixture
def trusted_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        PRODUCTION_TRUSTED_PROXY_IPS,
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


def test_normalize_ipv4_ipv6_and_mapped() -> None:
    assert normalize_client_source("203.0.113.1") == "203.0.113.1"
    assert normalize_client_source("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_client_source("2001:db8::1") == "2001:db8::1"
    assert normalize_client_source("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_client_source("[2001:db8::5]:443") == "2001:db8::5"
    assert normalize_client_source("not-an-ip") is None
    assert normalize_client_source("   ") is None


def test_direct_spoof_single_and_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    for headers in (
        {"X-Forwarded-For": "203.0.113.99"},
        {"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
        {"Forwarded": 'for="203.0.113.99"'},
        {"CF-Connecting-IP": "203.0.113.99"},
    ):
        request = _request_with_client("198.51.100.10", headers)
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path == "direct_peer"


def test_cloudflare_append_ignores_attacker_leftmost(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "x_forwarded_for"


def test_trusted_chain_resolves_expected_client(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "x_forwarded_for"


def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_settings,
) -> None:
    request = _request_with_client(
        "198.51.100.25",
        {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "198.51.100.25"
    assert resolution.path == "framework_peer"


def test_direct_render_origin_ignores_cf_connecting_ip(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {
            "CF-Connecting-IP": "203.0.113.77",
            "X-Forwarded-For": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path == "x_forwarded_for"

    spoof = _request_with_client(
        "127.0.0.1",
        {"CF-Connecting-IP": "203.0.113.88"},
    )
    spoof_resolution = resolve_admin_login_client_source(spoof, trusted_settings)
    assert spoof_resolution.path == "trusted_proxy_fallback"
    assert spoof_resolution.source == "unknown-trusted-proxy"


def test_header_precedence_xff_over_forwarded_and_cf(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {
            "X-Forwarded-For": "203.0.113.1, 10.0.0.2",
            "Forwarded": 'for="198.51.100.99"',
            "CF-Connecting-IP": "198.51.100.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.1"
    assert resolution.path == "x_forwarded_for"


def test_forwarded_header_used_when_xff_missing(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {"Forwarded": 'for="203.0.113.44";proto=https, for=10.0.0.5'},
    )
    resolution = resolve_admin_login_client_source(request, trusted_settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == "forwarded"


def test_address_format_edge_cases(trusted_settings) -> None:
    request = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": " , , 203.0.113.5 , 10.0.0.1 "},
    )
    assert resolve_admin_login_client_source(request, trusted_settings).source == "203.0.113.5"

    invalid = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": "totally-invalid, 10.0.0.1"},
    )
    assert resolve_admin_login_client_source(invalid, trusted_settings).path == (
        "trusted_proxy_fallback"
    )

    overlong = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": ", ".join(["203.0.113.1"] * 40)},
    )
    assert resolve_admin_login_client_source(overlong, trusted_settings).path == (
        "trusted_proxy_fallback"
    )


def test_missing_peer_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution == ClientSourceResolution(source="unknown", path="missing_peer")


def test_legacy_admin_trust_proxy_headers_applies_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": "203.0.113.60, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"


def test_invalid_forwarding_emits_sampled_telemetry(
    trusted_settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    request = _request_with_client(
        "127.0.0.1",
        {"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
    )
    resolve_admin_login_client_source(request, trusted_settings)
    assert any(
        record.getMessage() == "Admin login client source ignored forwarding data"
        for record in caplog.records
    )
    assert all(
        "203.0.113" not in (record.getMessage() + str(record.__dict__))
        for record in caplog.records
    )


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same ProxyHeadersMiddleware trusted-host config as production."""
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        PRODUCTION_TRUSTED_PROXY_IPS,
    )
    probe_app = FastAPI()

    @probe_app.post("/probe")
    def probe(request: Request) -> dict[str, str]:
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        return {"source": resolution.source, "path": resolution.path}

    wrapped = uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware(
        probe_app,
        trusted_hosts=PRODUCTION_TRUSTED_PROXY_IPS,
    )
    from starlette.testclient import TestClient

    client = TestClient(wrapped, client=("127.0.0.1", 50000))
    spoofed = client.post(
        "/probe",
        headers={"X-Forwarded-For": "203.0.113.77, 198.51.100.10"},
    )
    direct = client.post("/probe")

    assert spoofed.status_code == 200
    assert spoofed.json()["source"] == "198.51.100.10"
    assert direct.status_code == 200
    assert direct.json()["path"] == "trusted_proxy_fallback"
