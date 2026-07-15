"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    deployment_trust_flags,
    normalize_ip_address,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

RENDER_LB = "10.0.0.5"
CLOUDFLARE_EDGE = "173.245.48.10"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "203.0.113.88"
ATTACKER_DIRECT = "198.51.100.10"

TRUSTED_ENV = {
    "ADMIN_TRUST_PROXY_HEADERS": "true",
    "ADMIN_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,172.16.0.0/12",
    "ADMIN_TRUST_CLOUDFLARE_EDGE": "true",
    "UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.0/8,172.16.0.0/12",
}


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
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


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in TRUSTED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("203.0.113.1:not-a-port") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored_without_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    for header in (
        REAL_CLIENT,
        f"{REAL_CLIENT}, {OTHER_CLIENT}",
    ):
        request = _request_with_client(
            ATTACKER_DIRECT,
            headers={"X-Forwarded-For": header},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == ATTACKER_DIRECT
        assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "X-Forwarded-For": f"203.0.113.99, {REAL_CLIENT}, {CLOUDFLARE_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")
    settings = get_settings()
    request = _request_with_client(
        ATTACKER_DIRECT,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_DIRECT
    assert resolution.path is SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")
    settings = get_settings()
    request = _request_with_client(
        ATTACKER_DIRECT,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_DIRECT
    assert resolution.path is SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED


@pytest.mark.unit
def test_cf_connecting_ip_when_cloudflare_hop_present(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": CLOUDFLARE_EDGE,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_precedence_over_xff(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={
            "Forwarded": f'for={REAL_CLIENT};proto=https, for="{CLOUDFLARE_EDGE}"',
            "X-Forwarded-For": f"{OTHER_CLIENT}, {CLOUDFLARE_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_invalid_xff_chain_is_conservative(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": "bad-hop, also-bad"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_overlong_xff_chain_is_conservative(trusted_proxy_env: None) -> None:
    settings = get_settings()
    hops = ", ".join(f"10.0.0.{index}" for index in range(40))
    request = _request_with_client(RENDER_LB, headers={"X-Forwarded-For": hops})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_empty_xff_elements_are_invalid(trusted_proxy_env: None) -> None:
    settings = get_settings()
    request = _request_with_client(
        RENDER_LB,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, , {CLOUDFLARE_EDGE}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_source_key(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request_with_client(
                    RENDER_LB,
                    headers={"X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}"},
                ),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    trusted_proxy_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request_with_client(
        ATTACKER_DIRECT,
        headers={"X-Forwarded-For": REAL_CLIENT, "CF-Connecting-IP": REAL_CLIENT},
    )
    resolve_admin_login_client_source(request, settings)
    combined = caplog.text
    assert REAL_CLIENT not in combined
    assert "X-Forwarded-For" not in combined
    assert "cf-connecting-ip" not in combined.lower()


@pytest.mark.unit
def test_deployment_trust_flags_reflect_settings(
    trusted_proxy_env: None,
) -> None:
    settings = get_settings()
    flags = deployment_trust_flags(settings)
    assert flags == {
        "admin_trust_proxy_headers": True,
        "admin_trusted_proxy_cidrs_configured": True,
        "admin_trust_cloudflare_edge": True,
        "uvicorn_forwarded_allow_ips_configured": True,
    }



@pytest.mark.integration
def test_login_limiter_integration_with_trusted_proxy_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise login limiter through ASGI with a trusted Render peer address."""
    from tests.test_admin_auth import (  # noqa: PLC0415
        FakeRateLimitStore,
        LOGIN_FLOW_COOKIE_NAME,
        TEST_HASH,
        TEST_SECRET,
        TEST_USERNAME,
        _PeerOverrideMiddleware,
        _extract_csrf_token,
        _parse_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    store = FakeRateLimitStore()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    for key, value in TRUSTED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    peer_app = _PeerOverrideMiddleware(app, RENDER_LB)
    integration_client = TestClient(peer_app, follow_redirects=False)

    with shared_rate_limiter(store):
        with mock_db_connection():
            form = integration_client.get("/admin/login")
            csrf_token, cookies = _parse_login_form(form)
            headers = {"X-Forwarded-For": f"203.0.113.99, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"}

            first = integration_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers,
            )
            assert first.status_code == 401
            csrf_token = _extract_csrf_token(first.text)
            flow_cookie = first.cookies.get(LOGIN_FLOW_COOKIE_NAME)
            if flow_cookie:
                cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie

            second = integration_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"203.0.113.55, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
            )
            assert second.status_code == 401
            csrf_token = _extract_csrf_token(second.text)
            flow_cookie = second.cookies.get(LOGIN_FLOW_COOKIE_NAME)
            if flow_cookie:
                cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie

            blocked = integration_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"203.0.113.66, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
            )
            assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.integration
def test_uvicorn_start_command_includes_forwarded_allow_ips() -> None:
    render_yaml = Path("render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in render_yaml
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
