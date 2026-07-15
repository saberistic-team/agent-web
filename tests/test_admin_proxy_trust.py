"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    ClientSourceResolutionPath,
    TrustedProxyBoundary,
    normalize_client_address,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)

RENDER_LB = "10.0.0.1"
CF_EDGE = "173.245.48.1"
CLIENT = "203.0.113.50"
ATTACKER = "198.51.100.10"
OTHER_CLIENT = "203.0.113.77"

TRUSTED_PROXIES = f"{RENDER_LB},{CF_EDGE}/32"
CF_CIDRS = f"{CF_EDGE}/32"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _request(
    peer: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "headers": raw_headers,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", CF_CIDRS)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_proxy_trust_telemetry()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    settings = get_settings()
    for xff in ("203.0.113.99", "203.0.113.99, 10.0.0.1"):
        request = _request(ATTACKER, headers={"X-Forwarded-For": xff})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == ATTACKER
        assert resolution.path == ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    xff = f"203.0.113.99, {CLIENT}, {CF_EDGE}"
    request = _request(RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == ClientSourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    xff = f"{CLIENT}, {CF_EDGE}"
    request = _request(RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == ClientSourceResolutionPath.TRUSTED_XFF_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    xff = f"{CLIENT}, {RENDER_LB}"
    request = _request(ATTACKER, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER
    assert resolution.path == ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    request = _request(
        ATTACKER,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"{CLIENT}, {CF_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER
    assert resolution.path == ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cf_connecting_ip_when_cloudflare_verified_in_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    request = _request(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"203.0.113.99, {CF_EDGE}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == ClientSourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    request = _request(
        RENDER_LB,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"203.0.113.99, {CF_EDGE}",
            "Forwarded": f"for=198.51.100.60;proto=https;by={RENDER_LB}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == ClientSourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_no_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    request = _request(
        RENDER_LB,
        headers={"Forwarded": f"for={CLIENT};proto=https, for={RENDER_LB};proto=https"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == ClientSourceResolutionPath.TRUSTED_FORWARDED_HEADER


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_normalize_client_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_malformed_overlong_and_empty_xff_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    overlong = ",".join(["203.0.113.1"] * 60)
    request = _request(RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == ClientSourceResolutionPath.MALFORMED_FORWARDING
    assert resolution.source == RENDER_LB

    empty_elems = _request(RENDER_LB, headers={"X-Forwarded-For": f", {CLIENT}, ,"})
    resolution = resolve_admin_login_client_source(empty_elems, settings)
    assert resolution.source == CLIENT


@pytest.mark.unit
def test_missing_peer_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch)
    settings = get_settings()
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == "unknown"
    assert resolution.path == ClientSourceResolutionPath.UNKNOWN_PEER


@pytest.mark.unit
def test_untrusted_forwarding_emits_sampled_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request(ATTACKER, headers={"X-Forwarded-For": CLIENT})
    resolve_admin_login_client_source(request, settings)
    assert any(
        "untrusted forwarding" in record.message.lower()
        for record in caplog.records
    )
    for record in caplog.records:
        message = record.getMessage()
        assert CLIENT not in message
        assert ATTACKER not in message


@pytest.mark.unit
def test_trusted_proxy_boundary_matches_uvicorn_semantics() -> None:
    boundary = TrustedProxyBoundary(TRUSTED_PROXIES)
    xff = f"203.0.113.99, {CLIENT}, {CF_EDGE}"
    host, _ = boundary.client_from_x_forwarded_for(xff)  # type: ignore[misc]
    assert host == CLIENT


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import (
        FakeRateLimitStore,
        shared_rate_limiter,
        _login,
    )

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        for i in range(5):
            response = _login(
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            )
            assert response.status_code == 401

        blocked = _login(password="wrong", headers={"X-Forwarded-For": "203.0.113.99"})
        assert blocked.status_code == 429
        assert len(store.rows) == 2


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_limiter_uses_real_client_not_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", CF_CIDRS)

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXIES)
    transport = httpx.ASGITransport(app=wrapped, client=(RENDER_LB, 12345))
    store = FakeRateLimitStore()
    headers = {"X-Forwarded-For": f"203.0.113.99, {OTHER_CLIENT}, {CF_EDGE}"}

    async def _attempt_login(
        http: httpx.AsyncClient,
        *,
        request_headers: dict[str, str] | None = None,
    ) -> int:
        with mock_db_connection():
            login_page = await http.get("/admin/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        assert csrf_match
        with mock_db_connection():
            response = await http.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_match.group(1),
                },
                headers=request_headers or headers,
                follow_redirects=False,
            )
        return response.status_code

    with shared_rate_limiter(store):
        async def _run_all() -> None:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                assert await _attempt_login(http) == 401
                assert await _attempt_login(http) == 401
                assert await _attempt_login(http) == 429
                assert await _attempt_login(
                    http,
                    request_headers={
                        "X-Forwarded-For": f"203.0.113.88, {OTHER_CLIENT}, {CF_EDGE}",
                    },
                ) == 429

        asyncio.run(_run_all())


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise ProxyHeadersMiddleware + app login path like production Uvicorn."""
    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUSTED_CIDRS", CF_CIDRS)

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXIES)
    transport = httpx.ASGITransport(app=wrapped, client=(RENDER_LB, 12345))
    store = FakeRateLimitStore()

    async def _run_login_burst() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            with mock_db_connection():
                login_page = await http.get("/admin/login")
            assert login_page.status_code == 200
            csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
            assert csrf_match
            headers = {"X-Forwarded-For": f"203.0.113.1, {OTHER_CLIENT}, {CF_EDGE}"}
            for _ in range(2):
                with mock_db_connection():
                    response = await http.post(
                        "/admin/login",
                        data={
                            "username": "ghost",
                            "password": "wrong",
                            "csrf_token": csrf_match.group(1),
                        },
                        headers=headers,
                        follow_redirects=False,
                    )
                assert response.status_code == 401
                with mock_db_connection():
                    login_page = await http.get("/admin/login")
                csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
                assert csrf_match

            with mock_db_connection():
                blocked = await http.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_match.group(1),
                    },
                    headers=headers,
                    follow_redirects=False,
                )
            assert blocked.status_code == 429

    with shared_rate_limiter(store):
        asyncio.run(_run_login_burst())

    source_key = admin_auth.build_source_rate_limit_key(OTHER_CLIENT)
    assert source_key in store.rows


@pytest.mark.unit
def test_limiter_keys_and_logs_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter, _login

    _settings_env(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    caplog.set_level(logging.DEBUG)
    xff = f"203.0.113.99, {CLIENT}, {CF_EDGE}"
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        _login(username="ghost", password="wrong", headers={"X-Forwarded-For": xff})

    for limiter_key, row in store.rows.items():
        assert "x-forwarded-for" not in limiter_key.lower()
        assert "203.0.113" not in limiter_key
        assert len(limiter_key) == 64

    for record in caplog.records:
        message = record.getMessage()
        assert "x-forwarded-for" not in message.lower()
        assert CLIENT not in message
