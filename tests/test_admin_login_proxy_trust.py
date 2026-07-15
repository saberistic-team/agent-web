"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    RESOLUTION_AMBIGUOUS_FORWARDING,
    RESOLUTION_CF_CONNECTING_IP,
    RESOLUTION_DIRECT_PEER,
    RESOLUTION_FORWARDED_CHAIN,
    RESOLUTION_MALFORMED_FORWARDING,
    RESOLUTION_UNTRUSTED_FORWARDING,
    normalize_ip_address,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
)
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_HOP = "104.16.0.1"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
SPOOF = "198.51.100.99"

TRUSTED_CIDRS = "10.0.0.0/8,127.0.0.0/8"
CLOUDFLARE_CIDRS = "104.16.0.0/13"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in (headers or {}).items()
        ],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_CIDRS)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", CLOUDFLARE_CIDRS)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_proxy_trust_telemetry()


def _enable_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")


def _resolve(host: str, headers: dict[str, str] | None = None) -> str:
    settings = get_settings()
    return resolve_admin_login_client_source(
        _request_with_client(host, headers=headers),
        settings,
    ).source


@pytest.mark.unit
def test_normalize_ipv4_mapped_ipv6() -> None:
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    peer = "198.51.100.10"
    assert _resolve(peer, {"X-Forwarded-For": SPOOF}) == peer
    assert _resolve(peer, {"X-Forwarded-For": f"{SPOOF}, {CLIENT_A}"}) == peer


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    xff = f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_HOP}"
    assert _resolve(RENDER_PROXY, {"X-Forwarded-For": xff}) == CLIENT_A


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    xff = f"{CLIENT_A}, 10.0.0.2"
    assert _resolve(RENDER_PROXY, {"X-Forwarded-For": xff}) == CLIENT_A


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    # Untrusted peer with a forged multi-hop chain must not pick a header value.
    peer = "198.51.100.20"
    xff = f"{CLIENT_A}, {CLOUDFLARE_HOP}, {RENDER_PROXY}"
    assert _resolve(peer, {"X-Forwarded-For": xff}) == peer


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    peer = "198.51.100.30"
    headers = {
        "CF-Connecting-IP": CLIENT_A,
        "X-Forwarded-For": CLIENT_A,
    }
    assert _resolve(peer, headers) == peer


@pytest.mark.unit
def test_cf_connecting_ip_used_with_verified_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    xff = f"{SPOOF}, {CLOUDFLARE_HOP}"
    headers = {"X-Forwarded-For": xff, "CF-Connecting-IP": CLIENT_A}
    resolution = resolve_admin_login_client_source(
        _request_with_client(RENDER_PROXY, headers=headers),
        get_settings(),
    )
    assert resolution.source == CLIENT_A
    assert resolution.resolution_path == RESOLUTION_CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    forwarded = f'for="{CLIENT_A}", for="{CLOUDFLARE_HOP}"'
    assert _resolve(RENDER_PROXY, {"Forwarded": forwarded}) == CLIENT_A


@pytest.mark.unit
def test_xff_precedence_over_conflicting_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    headers = {
        "X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_HOP}",
        "Forwarded": f'for="{CLIENT_B}", for="{CLOUDFLARE_HOP}"',
    }
    assert _resolve(RENDER_PROXY, headers) == CLIENT_A


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    assert _resolve(RENDER_PROXY, {"X-Forwarded-For": "not-an-ip"}) == RENDER_PROXY
    long_chain = ", ".join(["203.0.113.1"] * 40)
    assert _resolve(RENDER_PROXY, {"X-Forwarded-For": long_chain}) == RENDER_PROXY


@pytest.mark.unit
def test_ambiguous_single_xff_hop_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    resolution = resolve_admin_login_client_source(
        _request_with_client(RENDER_PROXY, headers={"X-Forwarded-For": SPOOF}),
        get_settings(),
    )
    assert resolution.source == "unknown"
    assert resolution.resolution_path == RESOLUTION_AMBIGUOUS_FORWARDING


@pytest.mark.unit
def test_missing_peer_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_proxy_trust(monkeypatch)
    scope = {
        "type": "http",
        "headers": [],
        "client": None,
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), get_settings())
    assert resolution.source == "unknown"


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _enable_proxy_trust(monkeypatch)
    with shared_rate_limiter(rate_limit_store):
        peer_headers = {"X-Forwarded-For": SPOOF}
        assert _login(username="ghost", password="wrong", headers=peer_headers).status_code == 401
        assert _login(username="ghost", password="wrong", headers=peer_headers).status_code == 401
        blocked = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
    assert blocked.status_code == 429
    assert admin_auth.build_source_rate_limit_key(SPOOF) not in rate_limit_store.rows
    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_proxy_trust(monkeypatch)
    caplog.set_level(logging.DEBUG)
    resolve_admin_login_client_source(
        _request_with_client(
            "198.51.100.40",
            headers={"X-Forwarded-For": f"{SPOOF}, {CLIENT_A}"},
        ),
        get_settings(),
    )
    combined = caplog.text
    assert SPOOF not in combined
    assert CLIENT_A not in combined
    assert "resolution_path" in combined or "Admin login client source resolved" in combined


@pytest.mark.unit
def test_invalid_forwarding_warning_is_sampled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_proxy_trust(monkeypatch)
    caplog.set_level(logging.WARNING)
    request = _request_with_client("198.51.100.50", headers={"X-Forwarded-For": SPOOF})
    settings = get_settings()
    for _ in range(3):
        resolve_admin_login_client_source(request, settings)
    assert "Admin login forwarding headers ignored" in caplog.text
    assert SPOOF not in caplog.text


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
    admin_auth_doc = (repo_root / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")

    assert "--forwarded-allow-ips=127.0.0.1,::1" in render_yaml
    assert 'ADMIN_TRUST_PROXY_HEADERS' in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_CLOUDFLARE_PROXY_CIDRS" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in admin_auth_doc
    assert "--forwarded-allow-ips" in admin_auth_doc


@pytest.mark.unit
@pytest.mark.integration
def test_health_reports_proxy_trust_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"] == "configured"
    assert "127.0.0.1" not in str(payload)
    assert "104.16" not in str(payload)


@pytest.mark.integration
def test_proxy_headers_middleware_does_not_drive_limiter_identity(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    """Exercise ProxyHeadersMiddleware with production-like trust disabled for remote peers."""
    _enable_proxy_trust(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    class _SetPeerMiddleware:
        def __init__(self, inner: Any, host: str) -> None:
            self.inner = inner
            self.host = host

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope = {**scope, "client": (self.host, 12345)}
            await self.inner(scope, receive, send)

    wrapped = _SetPeerMiddleware(
        ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1"]),
        RENDER_PROXY,
    )
    client = TestClient(wrapped, follow_redirects=False)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            form = client.get("/admin/login")
            csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf_token is not None
            cookies = {admin_auth.LOGIN_FLOW_COOKIE_NAME: form.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]}
            headers = {"X-Forwarded-For": f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_HOP}"}
            for _ in range(2):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token.group(1),
                    },
                    cookies=cookies,
                    headers=headers,
                )
                assert response.status_code == 401
                csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
                assert csrf_match is not None
                cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = response.cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME]
                csrf_token = csrf_match

            blocked = client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token.group(1),
                },
                cookies=cookies,
                headers={"X-Forwarded-For": f"198.51.100.1, {CLIENT_A}, {CLOUDFLARE_HOP}"},
            )
    assert blocked.status_code == 429
    assert admin_auth.build_source_rate_limit_key(SPOOF) not in rate_limit_store.rows
    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
def test_resolution_paths_are_stable() -> None:
    assert RESOLUTION_DIRECT_PEER == "direct_peer"
    assert RESOLUTION_FORWARDED_CHAIN == "forwarded_chain"
    assert RESOLUTION_CF_CONNECTING_IP == "cf_connecting_ip"
    assert RESOLUTION_UNTRUSTED_FORWARDING == "untrusted_forwarding"
    assert RESOLUTION_MALFORMED_FORWARDING == "malformed_forwarding"
    assert RESOLUTION_AMBIGUOUS_FORWARDING == "ambiguous_forwarding"


@pytest.mark.unit
def test_whitespace_and_empty_xff_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    xff = f"  {CLIENT_A}  , , {CLOUDFLARE_HOP} "
    assert _resolve(RENDER_PROXY, {"X-Forwarded-For": xff}) == CLIENT_A


@pytest.mark.unit
def test_limiter_row_keys_contain_no_raw_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_proxy_trust(monkeypatch)
    source = _resolve(RENDER_PROXY, {"X-Forwarded-For": f"{CLIENT_A}, {CLOUDFLARE_HOP}"})
    key = admin_auth.build_source_rate_limit_key(source)
    assert CLIENT_A not in key
    assert re.fullmatch(r"[0-9a-f]{64}", key)
