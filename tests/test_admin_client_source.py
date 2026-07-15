"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import Request

pytest_plugins = ["tests.test_admin_auth"]

from app import admin_auth
from app.asgi import app as asgi_app
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.client_source import (
    DEFAULT_CLOUDFLARE_EDGE_CIDRS,
    RENDER_TRUSTED_PROXY_CIDRS,
    ClientSourceResolution,
    immediate_peer_host,
    normalize_ip_literal,
    proxy_trust_health_summary,
    reset_client_source_telemetry_for_tests,
    resolve_client_source,
)
from app.config import get_settings
from app.ip_networks import parse_networks
from tests.test_admin_auth import (
    FakeRateLimitStore,
    _extract_csrf_token,
    _login,
    _request_with_client,
    mock_db_connection,
    shared_rate_limiter,
)

RENDER_LB = "10.0.0.5"
CLOUDFLARE_EDGE = "173.245.48.10"
REAL_CLIENT = "203.0.113.50"
SPOOFED_CLIENT = "198.51.100.99"
UNTRUSTED_PEER = "203.0.113.77"


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trust: bool = True,
    trusted_proxy_ips: str = "10.0.0.0/8",
    cloudflare_edge_ips: str = ",".join(DEFAULT_CLOUDFLARE_EDGE_CIDRS),
) -> Any:
    if trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted_proxy_ips)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_EDGE_IPS", cloudflare_edge_ips)
    return get_settings()


def _request(
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    request = _request_with_client(peer)
    if headers:
        for name, value in headers.items():
            request.headers.__dict__["_list"].append(
                (name.lower().encode("ascii"), value.encode("ascii"))
            )
    return request


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    for header_value in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
    ):
        request = _request(
            UNTRUSTED_PEER,
            {"X-Forwarded-For": header_value},
        )
        assert resolve_client_source(request, settings) == ClientSourceResolution(
            source=UNTRUSTED_PEER,
            path="untrusted_forwarding_rejected",
            rejected_forwarding=True,
        )


@pytest.mark.unit
def test_cloudflare_append_preserves_attacker_leftmost_but_selects_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        {
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
        },
    )
    assert resolve_client_source(request, settings) == ClientSourceResolution(
        source=REAL_CLIENT,
        path="xff_right_to_left",
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        {
            "X-Forwarded-For": (
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_LB}"
            ),
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    assert resolve_client_source(request, settings) == ClientSourceResolution(
        source=REAL_CLIENT,
        path="cf_connecting_ip",
    )


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        UNTRUSTED_PEER,
        {
            "X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB}",
        },
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_forwarding_rejected"
    assert resolution.rejected_forwarding is True


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        UNTRUSTED_PEER,
        {
            "CF-Connecting-IP": SPOOFED_CLIENT,
            "CF-Ray": "fake-ray",
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {RENDER_LB}",
        },
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == "untrusted_forwarding_rejected"


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        {
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": f"{SPOOFED_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_LB}",
            "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
        },
    )
    assert resolve_client_source(request, settings).source == REAL_CLIENT
    assert resolve_client_source(request, settings).path == "cf_connecting_ip"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("x" * 200, None),
    ],
)
def test_normalize_ip_literal_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_literal(raw) == expected


@pytest.mark.unit
def test_excessive_forwarding_chain_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    long_chain = ", ".join([f"203.0.113.{index}" for index in range(1, 40)])
    request = _request(RENDER_LB, {"X-Forwarded-For": f"{long_chain}, {RENDER_LB}"})
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == RENDER_LB


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = _settings(monkeypatch)

    def _peer_from_test_header(request: Request) -> str | None:
        test_peer = request.headers.get("x-test-asgi-peer")
        if test_peer:
            return test_peer.strip()
        if request.client is not None:
            return request.client.host
        return None

    with (
        shared_rate_limiter(rate_limit_store),
        patch("app.client_source.immediate_peer_host", side_effect=_peer_from_test_header),
    ):
        for index in range(4):
            response = _login(
                username="ghost",
                password="wrong",
                headers={
                    "X-Test-Asgi-Peer": UNTRUSTED_PEER,
                    "X-Forwarded-For": f"203.0.113.{index}",
                },
            )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(UNTRUSTED_PEER)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _settings(monkeypatch)
    caplog.set_level(logging.INFO)

    with shared_rate_limiter(rate_limit_store):
        _login(
            password="wrong",
            headers={
                "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
                "CF-Connecting-IP": REAL_CLIENT,
            },
        )

    for record in caplog.records:
        message = record.getMessage()
        assert REAL_CLIENT not in message
        assert SPOOFED_CLIENT not in message
        assert "x-forwarded-for" not in message.lower()
        extra = getattr(record, "__dict__", {})
        for value in extra.values():
            if isinstance(value, str):
                assert REAL_CLIENT not in value
                assert SPOOFED_CLIENT not in value

    for row in rate_limit_store.rows.values():
        assert REAL_CLIENT not in str(row)
        assert SPOOFED_CLIENT not in str(row)


@pytest.mark.unit
@pytest.mark.integration
def test_asgi_stack_resolves_client_source_with_proxy_headers() -> None:
    """Exercise the production ASGI wrapper (peer capture + Uvicorn proxy headers)."""

    async def _request_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(
                "/health",
                headers={
                    "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
                },
                extensions={"asgi": {"client": (RENDER_LB, 12345)}},
            )

    response = asyncio.run(_request_health())
    assert response.status_code == 200
    payload = response.json()
    assert payload["admin_proxy_trust"]["proxy_headers_enabled"] is False
    assert "admin_proxy_trust" in payload


@pytest.mark.unit
@pytest.mark.integration
def test_asgi_stack_login_limiter_uses_peer_capture_not_spoofed_xff(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login limiter through production ASGI stack without resolver patches.

    httpx.ASGITransport always presents ``127.0.0.1`` as the TCP peer; rotating
    ``X-Forwarded-For`` values must not create separate source buckets anyway.
    """

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _settings(monkeypatch, trust=False)

    async def _run() -> list[int]:
        transport = httpx.ASGITransport(app=asgi_app)
        status_codes: list[int] = []
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            with shared_rate_limiter(rate_limit_store), mock_db_connection():
                login_page = await http_client.get("/admin/login")
                csrf_token = _extract_csrf_token(login_page.text)
                cookies = {
                    LOGIN_FLOW_COOKIE_NAME: login_page.cookies[LOGIN_FLOW_COOKIE_NAME],
                }
                for index in range(4):
                    response = await http_client.post(
                        "/admin/login",
                        data={
                            "username": "ghost",
                            "password": "wrong",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                        headers={"X-Forwarded-For": f"203.0.113.{index}"},
                    )
                    status_codes.append(response.status_code)
                    if response.status_code == 401:
                        csrf_token = _extract_csrf_token(response.text)
                        flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
                        if flow_cookie:
                            cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
        return status_codes

    status_codes = asyncio.run(_run())
    assert status_codes[:2] == [401, 401]
    assert status_codes[2] == 429
    assert len(rate_limit_store.rows) == 1
    peer_key = admin_auth.build_source_rate_limit_key("127.0.0.1")
    assert peer_key in rate_limit_store.rows
    for index in range(4):
        spoof_key = admin_auth.build_source_rate_limit_key(f"203.0.113.{index}")
        assert spoof_key not in rate_limit_store.rows


@pytest.mark.unit
def test_trust_disabled_uses_direct_peer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust=False)
    request = _request(
        UNTRUSTED_PEER,
        {
            "X-Forwarded-For": SPOOFED_CLIENT,
            "CF-Connecting-IP": SPOOFED_CLIENT,
        },
    )
    assert resolve_client_source(request, settings) == ClientSourceResolution(
        source=UNTRUSTED_PEER,
        path="direct_peer",
    )


@pytest.mark.unit
def test_forwarded_header_used_when_trusted_peer_and_no_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        {"Forwarded": f'for="{REAL_CLIENT}";proto=https'},
    )
    assert resolve_client_source(request, settings) == ClientSourceResolution(
        source=REAL_CLIENT,
        path="forwarded_header",
    )


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    assert resolve_client_source(request, settings) == ClientSourceResolution(
        source="unknown",
        path="missing_peer",
    )


@pytest.mark.unit
def test_trust_enabled_without_trusted_ips_rejects_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "")
    settings = get_settings()
    request = _request(RENDER_LB, {"X-Forwarded-For": SPOOFED_CLIENT})
    resolution = resolve_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path == "direct_peer"
    assert resolution.rejected_forwarding is True


@pytest.mark.unit
def test_malformed_xff_element_fails_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        {"X-Forwarded-For": f"not-an-ip, {REAL_CLIENT}, {RENDER_LB}"},
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == RENDER_LB


@pytest.mark.unit
def test_all_trusted_xff_chain_is_invalid_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, {"X-Forwarded-For": f"{RENDER_LB}, 10.0.0.6"})
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "invalid_forwarding"
    assert resolution.rejected_forwarding is True
    assert resolution.source == RENDER_LB


@pytest.mark.unit
def test_overlong_xff_header_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, {"X-Forwarded-For": "a" * 3000})
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == RENDER_LB


@pytest.mark.unit
def test_forwarded_header_skips_unknown_for_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, {"Forwarded": "for=unknown;proto=https"})
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "trusted_peer_fallback"
    assert resolution.source == RENDER_LB


@pytest.mark.unit
def test_immediate_peer_falls_back_to_request_client() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": (REAL_CLIENT, 443),
            "method": "GET",
            "path": "/",
        }
    )
    assert immediate_peer_host(request) == REAL_CLIENT


@pytest.mark.unit
def test_parse_networks_accepts_host_ips_and_skips_invalid() -> None:
    networks = parse_networks("10.0.0.5, 10.0.0.0/8, not-a-network")
    assert len(networks) == 2
    assert str(networks[0]) == "10.0.0.5/32"
    assert str(networks[1]) == "10.0.0.0/8"


@pytest.mark.unit
def test_asgi_forwarded_allow_hosts_falls_back_to_trusted_proxy_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    from app.asgi import _forwarded_allow_hosts

    assert _forwarded_allow_hosts() == ["10.0.0.0/8"]


@pytest.mark.unit
def test_proxy_trust_health_summary_reports_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    summary = proxy_trust_health_summary(settings)
    assert summary["proxy_headers_enabled"] is True
    assert summary["trusted_proxy_network_count"] == 1
    assert summary["cloudflare_edge_network_count"] > 0
    assert summary["forwarded_allow_ips_configured"] is False


@pytest.mark.unit
def test_normalize_ip_literal_rejects_invalid_bracket_suffix() -> None:
    assert normalize_ip_literal("[2001:db8::1]invalid") is None
