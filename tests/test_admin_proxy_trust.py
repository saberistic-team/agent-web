"""Trusted-proxy admin login source resolution (#239)."""

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
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_client_address,
    parse_trusted_proxy_cidrs,
    resolve_admin_login_client_source,
    resolve_from_forwarded_header,
    resolve_from_x_forwarded_for,
)
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_SECRET,
    TEST_USERNAME,
    _login,
    _login_flows,
    _request_with_client,
    _session_store,
    mock_db_connection,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PRIVATE_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
RENDER_LB = "10.0.0.2"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror ``test_admin_auth.admin_env`` for integration helpers imported here."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()
    _session_store.clear()


def _trusted_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_LB)
    return get_settings()


def _resolution(request: Request, settings: Any) -> ClientSourceResolution:
    return resolve_admin_login_client_source(request, settings)


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    for header in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
    ):
        request = _request_with_client(REAL_CLIENT)
        request.headers.__dict__["_list"].append((b"x-forwarded-for", header.encode()))
        assert _resolution(request, settings) == ClientSourceResolution(
            REAL_CLIENT,
            SourceResolutionPath.DIRECT_PEER,
        )


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append(
        (
            b"x-forwarded-for",
            f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}".encode(),
        )
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        REAL_CLIENT,
        SourceResolutionPath.TRUSTED_X_FORWARDED_FOR,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_LB}".encode())
    )
    assert _resolution(request, settings).source_material == REAL_CLIENT


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    untrusted_intermediary = "198.51.100.10"
    request = _request_with_client(untrusted_intermediary)
    request.headers.__dict__["_list"].append(
        (
            b"x-forwarded-for",
            f"{REAL_CLIENT}, {RENDER_LB}".encode(),
        )
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        untrusted_intermediary,
        SourceResolutionPath.UNTRUSTED_PEER,
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    direct_peer = "198.51.100.50"
    request = _request_with_client(direct_peer)
    request.headers.__dict__["_list"].append((b"cf-connecting-ip", REAL_CLIENT.encode()))
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode())
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        direct_peer,
        SourceResolutionPath.UNTRUSTED_PEER,
    )


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append((b"cf-connecting-ip", REAL_CLIENT.encode()))
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {RENDER_LB}".encode())
    )
    request.headers.__dict__["_list"].append(
        (b"forwarded", f'for={SPOOFED_CLIENT};proto=https'.encode())
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        REAL_CLIENT,
        SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
    )


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append(
        (b"forwarded", f"for={REAL_CLIENT};proto=https".encode())
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        REAL_CLIENT,
        SourceResolutionPath.TRUSTED_FORWARDED_HEADER,
    )


@pytest.mark.unit
def test_address_format_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _trusted_settings(monkeypatch)
    trusted = parse_trusted_proxy_cidrs(RENDER_LB)

    assert resolve_from_x_forwarded_for("", trusted) is None
    assert resolve_from_x_forwarded_for("   ,  , ", trusted) is None
    assert resolve_from_x_forwarded_for("not-an-ip, 10.0.0.2", trusted) is None

    overlong = ", ".join([f"10.0.0.{i}" for i in range(40)])
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append((b"x-forwarded-for", overlong.encode()))
    assert _resolution(request, settings).path == SourceResolutionPath.CHAIN_TOO_LONG

    ipv6_request = _request_with_client(RENDER_LB)
    ipv6_request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", b"[2001:db8::1], 10.0.0.2")
    )
    assert _resolution(ipv6_request, settings).source_material == "2001:db8::1"


@pytest.mark.unit
def test_resolve_from_forwarded_header_parses_quoted_and_bracketed() -> None:
    assert resolve_from_forwarded_header(f'for="{REAL_CLIENT}";proto=https') == REAL_CLIENT
    assert resolve_from_forwarded_header("for=[2001:db8::1];proto=https") == "2001:db8::1"
    assert resolve_from_forwarded_header("for=_hidden;proto=https") is None


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_source_buckets(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "testclient")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    stable_client = "203.0.113.50"
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            response = _login(
                username=f"rotator-{index}",
                password="wrong",
                headers={
                    "X-Forwarded-For": f"203.0.113.{index}, {stable_client}, testclient",
                },
            )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(stable_client)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_unknown_peer_when_client_missing() -> None:
    scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    settings = get_settings()
    assert _resolution(request, settings) == ClientSourceResolution(
        "unknown",
        SourceResolutionPath.UNKNOWN_PEER,
    )


@pytest.mark.unit
def test_trust_enabled_without_configured_cidrs_uses_direct_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{REAL_CLIENT}, {RENDER_LB}".encode())
    )
    assert _resolution(request, settings) == ClientSourceResolution(
        RENDER_LB,
        SourceResolutionPath.UNTRUSTED_PEER,
    )


@pytest.mark.unit
def test_invalid_forwarded_headers_fall_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(RENDER_LB)
    request.headers.__dict__["_list"].append((b"x-forwarded-for", b"not-an-ip, 10.0.0.2"))
    assert _resolution(request, settings) == ClientSourceResolution(
        RENDER_LB,
        SourceResolutionPath.INVALID_FORWARDED,
    )


@pytest.mark.unit
def test_trusted_proxy_boundary_supports_literals_and_cidrs() -> None:
    boundary = parse_trusted_proxy_cidrs("testclient,10.0.0.0/8")
    assert boundary.trusts("testclient")
    assert boundary.trusts("10.0.0.9")
    assert not boundary.trusts("203.0.113.1")


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")

    assert "startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers" in render_yaml
    assert "--forwarded-allow-ips=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" in render_yaml
    assert 'key: ADMIN_TRUST_PROXY_HEADERS\n        value: "true"' in render_yaml
    assert (
        'key: ADMIN_TRUSTED_PROXY_CIDRS\n        value: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"'
        in render_yaml
    )

    for cidr in RENDER_PRIVATE_CIDRS.split(","):
        assert cidr in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in docs
    assert "--forwarded-allow-ips" in docs


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client("198.51.100.10")
    request.headers.__dict__["_list"].append(
        (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode())
    )
    request.headers.__dict__["_list"].append((b"cf-connecting-ip", REAL_CLIENT.encode()))

    with caplog.at_level(logging.INFO, logger="app.proxy_trust"):
        resolve_admin_login_client_source(request, settings)

    combined = " ".join(record.getMessage() for record in caplog.records)
    assert SPOOFED_CLIENT not in combined
    assert REAL_CLIENT not in combined
    assert "x-forwarded-for" not in combined.lower()

    extras = [getattr(record, "__dict__", {}) for record in caplog.records]
    for extra in extras:
        for value in extra.values():
            if isinstance(value, str):
                assert SPOOFED_CLIENT not in value
                assert REAL_CLIENT not in value

    source_key = admin_auth.build_source_rate_limit_key("198.51.100.10")
    assert SPOOFED_CLIENT not in source_key
    assert REAL_CLIENT not in source_key
    assert re.fullmatch(r"[0-9a-f]{64}", source_key)


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_login_limiter_resolution(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same ASGI proxy middleware configuration used in deployment."""

    trusted_allow_ips = RENDER_PRIVATE_CIDRS
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted_allow_ips)
    proxy_client = TestClient(
        wrapped,
        follow_redirects=False,
        client=(RENDER_LB, 12345),
    )

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_allow_ips)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")

    def _post_login(*, xff: str, username: str = "ghost") -> Any:
        with mock_db_connection():
            form = proxy_client.get("/admin/login")
            csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf_match is not None
            return proxy_client.post(
                "/admin/login",
                data={
                    "username": username,
                    "password": "wrong-password",
                    "csrf_token": csrf_match.group(1),
                },
                headers={"X-Forwarded-For": xff},
            )

    with shared_rate_limiter(rate_limit_store), mock_db_connection():
        first = _post_login(xff=f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}")
        second = _post_login(xff=f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}")
        third = _post_login(
            xff=f"203.0.113.88, {SPOOFED_CLIENT}, {REAL_CLIENT}, {RENDER_LB}",
        )

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert len(rate_limit_store.rows) == 1
    assert admin_auth.build_source_rate_limit_key(REAL_CLIENT) in rate_limit_store.rows
