"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from fastapi import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_ip_address,
    parse_trusted_proxy_networks,
    resolve_admin_login_client_source,
    resolve_from_x_forwarded_for,
)
from app.config import Settings, get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    _login,
    _parse_login_form,
    _request_with_client,
    mock_db_connection,
    shared_rate_limiter,
)

RENDER_TRUSTED_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "fd00::/8",
)
RENDER_TRUSTED_NETWORKS = parse_trusted_proxy_networks(RENDER_TRUSTED_CIDRS)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


def _settings(*, trusted_cidrs: tuple[str, ...] = ()) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_trusted_proxy_cidrs=trusted_cidrs,
    )


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("latin1"), value.encode("latin1"))


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", ",".join(RENDER_TRUSTED_CIDRS))


@pytest.mark.unit
def test_non_ip_direct_peer_preserved_for_test_client() -> None:
    settings = _settings()
    request = _request_with_client("testclient")
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "testclient"
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_x_forwarded_for_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = _settings()

    single = _request_with_client(
        "198.51.100.10",
        headers=[_header("x-forwarded-for", "203.0.113.99")],
    )
    multi = _request_with_client(
        "198.51.100.10",
        headers=[_header("x-forwarded-for", "203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )

    assert resolve_admin_login_client_source(single, settings).address == "198.51.100.10"
    assert resolve_admin_login_client_source(multi, settings).address == "198.51.100.10"
    assert resolve_admin_login_client_source(single, settings).path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    trusted_proxy_env: None,
) -> None:
    settings = _settings(trusted_cidrs=RENDER_TRUSTED_CIDRS)
    request = _request_with_client(
        "10.0.0.1",
        headers=[_header("x-forwarded-for", "203.0.113.99, 198.51.100.10, 10.0.0.1")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "198.51.100.10"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: None) -> None:
    settings = _settings(trusted_cidrs=RENDER_TRUSTED_CIDRS)
    request = _request_with_client(
        "10.0.0.1",
        headers=[
            _header("cf-connecting-ip", "198.51.100.55"),
            _header("x-forwarded-for", "198.51.100.55, 10.0.0.1"),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "198.51.100.55"
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trusted_cidrs=("10.0.0.0/8",))
    request = _request_with_client(
        "203.0.113.50",
        headers=[_header("x-forwarded-for", "198.51.100.10, 203.0.113.50")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.50"
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", ",".join(RENDER_TRUSTED_CIDRS))
    settings = _settings(trusted_cidrs=RENDER_TRUSTED_CIDRS)
    request = _request_with_client(
        "203.0.113.77",
        headers=[
            _header("cf-connecting-ip", "198.51.100.99"),
            _header("x-forwarded-for", "198.51.100.99"),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "203.0.113.77"
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_multiple_header_families_precedence() -> None:
    settings = _settings(trusted_cidrs=RENDER_TRUSTED_CIDRS)
    request = _request_with_client(
        "10.0.0.1",
        headers=[
            _header("cf-connecting-ip", "198.51.100.1"),
            _header("x-forwarded-for", "203.0.113.9, 10.0.0.1"),
            _header("forwarded", 'for="203.0.113.8";proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(request, settings).address == "198.51.100.1"

    without_cf = _request_with_client(
        "10.0.0.1",
        headers=[
            _header("x-forwarded-for", "203.0.113.9, 10.0.0.1"),
            _header("forwarded", 'for="203.0.113.8";proto=https'),
        ],
    )
    assert resolve_admin_login_client_source(without_cf, settings).address == "203.0.113.9"

    only_forwarded = _request_with_client(
        "10.0.0.1",
        headers=[_header("forwarded", 'for="203.0.113.7";proto=https')],
    )
    result = resolve_admin_login_client_source(only_forwarded, settings)
    assert result.address == "203.0.113.7"
    assert result.path is SourceResolutionPath.FORWARDED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.999.999", None),
    ],
)
def test_address_format_normalization(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_x_forwarded_for_whitespace_empty_and_excessive_chain() -> None:
    assert (
        resolve_from_x_forwarded_for(
            " 203.0.113.1 , , 10.0.0.1 ",
            trusted_networks=RENDER_TRUSTED_NETWORKS,
        )
        is None
    )
    long_chain = ", ".join(["203.0.113.1"] * 40)
    assert (
        resolve_from_x_forwarded_for(long_chain, trusted_networks=RENDER_TRUSTED_NETWORKS)
        is None
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()

    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            response = _login(
                username=f"user-{index}",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            if index < 3:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_rows_contain_no_raw_forwarding_data(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    admin_auth.reset_login_rate_limiter()

    request = _request_with_client(
        "10.0.0.1",
    )
    request.headers.__dict__["_list"].extend(
        [
            (b"x-forwarded-for", b"203.0.113.44, 10.0.0.1"),
            (b"cf-connecting-ip", b"203.0.113.44"),
        ]
    )
    settings = get_settings()

    with shared_rate_limiter(rate_limit_store):
        with caplog.at_level(logging.INFO):
            admission = admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    assert admission.admitted is True
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.44")
    assert source_key in rate_limit_store.rows
    for row in rate_limit_store.rows.values():
        assert "203.0.113" not in str(row)
        assert "x-forwarded-for" not in str(row).lower()

    for record in caplog.records:
        message = record.getMessage().lower()
        assert "203.0.113" not in message
        assert "x-forwarded-for" not in message
        assert "cf-connecting-ip" not in message


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_middleware_integration_resolves_trusted_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PeerInjector:
        def __init__(self, inner: Any, peer_host: str) -> None:
            self._inner = inner
            self._peer_host = peer_host

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope = dict(scope)
                scope["client"] = (self._peer_host, 12345)
            await self._inner(scope, receive, send)

    proxy_app = ProxyHeadersMiddleware(
        PeerInjector(app, "10.0.0.1"),
        trusted_hosts="10.0.0.0/8",
    )

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=proxy_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            with mock_db_connection():
                form = await ac.get("/admin/login")
                csrf_token, _ = _parse_login_form(form)
                response = await ac.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    headers={
                        "X-Forwarded-For": "203.0.113.99, 198.51.100.20, 10.0.0.1",
                        "CF-Connecting-IP": "198.51.100.20",
                    },
                )
                assert response.status_code == 401

                settings = get_settings()
                probe_request = _request_with_client("10.0.0.1")
                probe_request.headers.__dict__["_list"].extend(
                    [
                        (b"x-forwarded-for", b"203.0.113.99, 198.51.100.20, 10.0.0.1"),
                        (b"cf-connecting-ip", b"198.51.100.20"),
                    ]
                )
                assert admin_auth.client_ip(probe_request, settings) == "198.51.100.20"

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", ",".join(RENDER_TRUSTED_CIDRS))
    asyncio.run(_exercise())
