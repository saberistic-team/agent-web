"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin_auth import build_source_rate_limit_key, try_admit_login_attempt
from app.admin_client_source import (
    ClientSourceResolution,
    SourceResolutionPath,
    client_ip,
    is_trusted_proxy_host,
    normalize_client_address,
    parse_trusted_proxy_entries,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings

RENDER_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"


def _settings_with_trusted_proxies(
    monkeypatch: pytest.MonkeyPatch,
    trusted: str = RENDER_TRUSTED_PROXIES,
) -> Settings:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted)
    return get_settings()


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("latin1"), value.encode("latin1"))
        for name, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("x" * 254) is None


@pytest.mark.unit
def test_parse_trusted_proxy_entries_accepts_cidrs() -> None:
    entries = parse_trusted_proxy_entries("10.0.0.1,10.0.0.0/8,2001:db8::/32")
    assert len(entries) == 3


@pytest.mark.unit
def test_direct_spoof_ignores_single_and_multi_value_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        source="198.51.100.10",
        path=SourceResolutionPath.DIRECT_PEER,
        untrusted_forwarding_detected=True,
    )

    request_multi = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    resolution_multi = resolve_admin_login_client_source(request_multi, settings)
    assert resolution_multi.source == "198.51.100.10"
    assert resolution_multi.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.77"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.77"
    assert resolution.path == SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.2, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="10.0.0.5")
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER
    assert resolution.untrusted_forwarding_detected is True


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    request = _request(
        peer="198.51.100.10",
        headers={
            "CF-Connecting-IP": "203.0.113.99",
            "True-Client-IP": "203.0.113.88",
            "X-Real-IP": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_xff_over_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.10, 10.0.0.5",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={"Forwarded": 'for="203.0.113.44";proto=https, for="10.0.0.5"'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_conflicting_header_families_follow_documented_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.10, 10.0.0.5",
            "Forwarded": 'for="203.0.113.55"',
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    assert resolve_admin_login_client_source(request, settings).source == "203.0.113.10"

    request_forwarded_only = _request(
        peer="10.0.0.5",
        headers={
            "Forwarded": 'for="203.0.113.66", for="10.0.0.5"',
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    assert (
        resolve_admin_login_client_source(request_forwarded_only, settings).source
        == "203.0.113.66"
    )


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_chains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    malformed = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "not-an-ip, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(malformed, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path == SourceResolutionPath.MALFORMED_FORWARDED

    overlong = ", ".join(["203.0.113.1"] * 40)
    overlong_request = _request(peer="10.0.0.5", headers={"X-Forwarded-For": overlong})
    overlong_resolution = resolve_admin_login_client_source(overlong_request, settings)
    assert overlong_resolution.source == "10.0.0.5"
    assert overlong_resolution.path == SourceResolutionPath.MALFORMED_FORWARDED


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    request = _request(peer=None, headers={"X-Forwarded-For": "203.0.113.1"})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.UNKNOWN_PEER


@pytest.mark.unit
def test_client_ip_wrapper_matches_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.5"},
    )
    assert client_ip(request, settings) == "203.0.113.50"


@pytest.mark.unit
def test_is_trusted_proxy_host_matches_cidr() -> None:
    entries = parse_trusted_proxy_entries("10.0.0.0/8")
    assert is_trusted_proxy_host("10.1.2.3", entries)
    assert not is_trusted_proxy_host("203.0.113.1", entries)


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_source_bucket(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "")
    settings = get_settings()
    keys: set[str] = set()
    with caplog.at_level(logging.INFO):
        for index in range(5):
            request = _request(
                peer="198.51.100.10",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            keys.add(build_source_rate_limit_key(client_ip(request, settings)))

    assert len(keys) == 1
    assert not any("203.0.113." in record.message for record in caplog.records)
    assert not any(
        getattr(record, "client_ip", None) for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same ProxyHeadersMiddleware configuration used in deployment."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXIES)
    settings = get_settings()

    captured: dict[str, str] = {}

    async def _capture(scope, receive, send):  # noqa: ANN001
        request = Request(scope, receive)
        captured["source"] = resolve_admin_login_client_source(request, settings).source
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ProxyHeadersMiddleware(
        _capture,
        trusted_hosts=RENDER_TRUSTED_PROXIES,
    )

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [
            (b"x-forwarded-for", b"203.0.113.99, 198.51.100.77"),
            (b"host", b"saberistic.com"),
        ],
        "client": ("10.0.0.5", 12345),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():  # noqa: ANN202
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # noqa: ANN001, ANN202
        return None

    asyncio.run(middleware(scope, receive, send))

    assert captured["source"] == "198.51.100.77"


@pytest.mark.unit
def test_telemetry_contains_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "")
    settings = get_settings()
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )

    class _Conn:
        def __enter__(self):  # noqa: ANN204
            raise RuntimeError("db unavailable")

        def __exit__(self, *args):  # noqa: ANN002, ANN204
            return False

    with caplog.at_level(logging.INFO):
        with patch("app.admin_auth.db.db_connection", lambda *_a, **_k: _Conn()):
            try_admit_login_attempt(request, settings, username="ghost")

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "203.0.113.99" not in joined
    assert "198.51.100.10" not in joined
    assert "x-forwarded-for" not in joined.lower()
    assert any(
        getattr(record, "source_resolution_path", None) == SourceResolutionPath.DIRECT_PEER.value
        for record in caplog.records
    )
