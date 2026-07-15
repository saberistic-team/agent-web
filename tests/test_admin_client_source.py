"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

pytest_plugins = ["tests.test_admin_auth"]

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_client_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    _login,
    _request_with_client,
    mock_db_connection,
    shared_rate_limiter,
)

TRUSTED_PROXY_CIDRS = "10.0.0.0/8,127.0.0.1/32"
CLOUDFLARE_PROXY_CIDRS = "103.21.244.0/22"
RENDER_PROXY = "10.0.0.5"
CLOUDFLARE_EDGE = "103.21.244.50"
REAL_CLIENT = "198.51.100.42"
SPOOFED_CLIENT = "203.0.113.99"


def _settings_with_proxy_trust(**overrides: Any) -> Settings:
    base = get_settings()
    fields = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "plausible_domain": base.plausible_domain,
        "plausible_api_key": base.plausible_api_key,
        "analytics_environment": base.analytics_environment,
        "admin_username": base.admin_username,
        "admin_password_hash": base.admin_password_hash,
        "admin_session_secret": base.admin_session_secret,
        "admin_session_ttl_seconds": base.admin_session_ttl_seconds,
        "admin_login_rate_limit": base.admin_login_rate_limit,
        "admin_login_rate_window_seconds": base.admin_login_rate_window_seconds,
        "admin_login_lockout_seconds": base.admin_login_lockout_seconds,
        "admin_trust_proxy_headers": True,
        "admin_trusted_proxy_cidrs": tuple(TRUSTED_PROXY_CIDRS.split(",")),
        "admin_cloudflare_proxy_cidrs": tuple(CLOUDFLARE_PROXY_CIDRS.split(",")),
        "audit_page_size": base.audit_page_size,
        "brief_page_size": base.brief_page_size,
    }
    fields.update(overrides)
    return Settings(**fields)


def _request(
    peer: str,
    *,
    headers: dict[str, str] | None = None,
    settings: Settings | None = None,
) -> Request:
    request = _request_with_client(peer)
    if headers:
        for key, value in headers.items():
            request.headers.__dict__["_list"].append((key.lower().encode(), value.encode()))
    return request


def _resolve(peer: str, *, headers: dict[str, str] | None = None) -> str:
    settings = _settings_with_proxy_trust()
    return resolve_admin_login_client_source(
        _request(peer, headers=headers),
        settings,
    ).source


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored() -> None:
    assert _resolve("198.51.100.10", headers={"X-Forwarded-For": SPOOFED_CLIENT}) == "198.51.100.10"


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored() -> None:
    headers = {"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"}
    assert _resolve("198.51.100.10", headers=headers) == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_ignores_spoofed_leftmost() -> None:
    headers = {
        "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
    }
    assert _resolve(RENDER_PROXY, headers=headers) == REAL_CLIENT


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    headers = {
        "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}",
        "CF-Connecting-IP": REAL_CLIENT,
    }
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    headers = {"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}"}
    assert _resolve("198.51.100.10", headers=headers) == "198.51.100.10"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    headers = {
        "CF-Connecting-IP": SPOOFED_CLIENT,
        "X-Forwarded-For": SPOOFED_CLIENT,
    }
    assert _resolve("198.51.100.10", headers=headers) == "198.51.100.10"


@pytest.mark.unit
def test_header_precedence_cf_when_cloudflare_hop_proven() -> None:
    headers = {
        "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_PROXY}",
        "CF-Connecting-IP": REAL_CLIENT,
        "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
    }
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_when_cf_absent() -> None:
    headers = {
        "X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_PROXY}",
        "Forwarded": f'for="{SPOOFED_CLIENT}";proto=https',
    }
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_forwarded_header_fallback_without_xff() -> None:
    headers = {"Forwarded": f'for="{REAL_CLIENT}";proto=https'}
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.FORWARDED_HEADER


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
    ],
)
def test_normalize_client_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_invalid_xff_entry_falls_back_to_peer() -> None:
    headers = {"X-Forwarded-For": "bad-ip, 10.0.0.5"}
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == RENDER_PROXY
    assert resolution.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_overlong_xff_chain_falls_back_to_peer() -> None:
    chain = ", ".join(f"203.0.113.{i}" for i in range(40))
    headers = {"X-Forwarded-For": f"{chain}, {RENDER_PROXY}"}
    resolution = resolve_admin_login_client_source(
        _request(RENDER_PROXY, headers=headers),
        _settings_with_proxy_trust(),
    )
    assert resolution.source == RENDER_PROXY
    assert resolution.path == SourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    with shared_rate_limiter(rate_limit_store):
        for i in range(3):
            headers = {"X-Forwarded-For": f"203.0.113.{i}"}
            response = _login(password="wrong", headers=headers)
            assert response.status_code == 401

        blocked = _login(password="wrong", headers={"X-Forwarded-For": "203.0.113.99"})
        assert blocked.status_code == 429

        source_keys = [
            key
            for key in rate_limit_store.rows
            if key == admin_auth.build_source_rate_limit_key("testclient")
        ]
        assert len(source_keys) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_rate_limit_uses_real_client_not_spoof(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    from fastapi.testclient import TestClient

    async def _asgi_with_peer(scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http":
            scope = {**scope, "client": (RENDER_PROXY, 12345)}
        await app(scope, receive, send)

    proxy_client = TestClient(_asgi_with_peer, follow_redirects=False)

    def _proxy_login(**kwargs: Any) -> Any:
        with mock_db_connection():
            form = proxy_client.get("/admin/login")
            csrf = _extract_csrf(form.text)
            flow_cookie = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
            data = {
                "username": kwargs.get("username", TEST_USERNAME),
                "password": kwargs.get("password", "wrong"),
                "csrf_token": csrf,
            }
            return proxy_client.post(
                "/admin/login",
                data=data,
                cookies={admin_auth.LOGIN_FLOW_COOKIE_NAME: flow_cookie},
                headers=kwargs.get("headers", {}),
            )

    with shared_rate_limiter(rate_limit_store):
        headers = {"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}"}
        assert _proxy_login(headers=headers).status_code == 401
        assert _proxy_login(headers=headers).status_code == 401
        assert _proxy_login(headers=headers).status_code == 429

        other_spoof_headers = {
            "X-Forwarded-For": f"203.0.113.88, {REAL_CLIENT}, {RENDER_PROXY}",
        }
        assert _proxy_login(headers=other_spoof_headers).status_code == 429


def _extract_csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    headers = {"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}"}
    resolve_admin_login_client_source(
        _request("198.51.100.10", headers=headers),
        _settings_with_proxy_trust(),
    )
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED_CLIENT not in message
        assert REAL_CLIENT not in message
        assert "x-forwarded-for" not in message.lower()
        if hasattr(record, "source_resolution_path"):
            assert SPOOFED_CLIENT not in str(record.source_resolution_path)


@pytest.mark.unit
def test_rate_limit_rows_store_only_digests(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    with shared_rate_limiter(rate_limit_store):
        _login(password="wrong", headers={"X-Forwarded-For": SPOOFED_CLIENT})
    for key in rate_limit_store.rows:
        assert SPOOFED_CLIENT not in key
        assert len(key) == 64


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_with_app_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Uvicorn ProxyHeadersMiddleware + app resolver (deployment shape)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", CLOUDFLARE_PROXY_CIDRS)

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXY_CIDRS)
    proxy_client = TestClient(wrapped, follow_redirects=False)
    headers = {
        "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_PROXY}",
    }

    with mock_db_connection():
        response = proxy_client.get("/admin/login", headers=headers)
    assert response.status_code in {200, 303, 401, 503}

    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", headers["X-Forwarded-For"].encode())],
        "client": (SPOOFED_CLIENT, 12345),
        "method": "GET",
        "path": "/admin/login",
    }
    request = Request(scope)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
