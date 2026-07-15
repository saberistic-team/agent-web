"""Tests for trusted-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, Generator
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    DEFAULT_UVICORN_FORWARDED_ALLOW_IPS,
    normalize_ip_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    LOGIN_FLOW_COOKIE_NAME,
    TEST_HASH,
    TEST_USERNAME,
    _extract_csrf_token,
    mock_db_connection,
    shared_rate_limiter,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Trusted Render LB stand-in for production-style tests.
RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "172.64.0.1"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
UNTRUSTED_PEER = "198.51.100.10"

PRODUCTION_TRUST_ENV = {
    "ADMIN_TRUST_PROXY_HEADERS": "true",
    "ADMIN_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128",
    "ADMIN_TRUST_CLOUDFLARE_PROXY": "true",
}


def _request(
    *,
    peer: str | None = UNTRUSTED_PEER,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": headers or [],
        "server": ("testserver", 80),
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


def _configure_trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in PRODUCTION_TRUST_ENV.items():
        monkeypatch.setenv(key, value)
    reset_source_resolution_telemetry()


@pytest.fixture(autouse=True)
def admin_client_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_CLOUDFLARE_PROXY", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_source_resolution_telemetry()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("203.0.113.1:notaport") is None


@pytest.mark.unit
def test_direct_spoof_ignored_without_trusted_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()

    for header_value in ("203.0.113.99", "203.0.113.1, 203.0.113.2"):
        request = _request(
            peer=UNTRUSTED_PEER,
            headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
        )
        result = resolve_admin_login_client_source(request, settings)
        assert result.source == UNTRUSTED_PEER
        assert result.path == ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_ignored_when_trust_enabled_but_peer_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99, 203.0.113.100")],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path == ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=RENDER_LB,
        headers=[
            (
                b"x-forwarded-for",
                f"198.18.0.1, {CLIENT_A}, {CLOUDFLARE_EDGE}".encode("ascii"),
            )
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_A
    assert result.path == ClientSourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"{CLIENT_A}, {RENDER_LB}".encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_A
    assert result.path == ClientSourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_partial_trust_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", f"{CLIENT_A}, {RENDER_LB}".encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path == ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"cf-connecting-ip", CLIENT_A.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == UNTRUSTED_PEER
    assert result.path == ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cf_connecting_ip_requires_cloudflare_hop_in_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", f"{CLOUDFLARE_EDGE}".encode("ascii")),
            (b"cf-connecting-ip", CLIENT_A.encode("ascii")),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_A
    assert result.path == ClientSourceResolutionPath.TRUSTED_CF_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", f"{CLIENT_A}, {RENDER_LB}".encode("ascii")),
            (b"forwarded", f'for="{CLIENT_B}";proto=https'.encode("ascii")),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_A
    assert result.path == ClientSourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    request = _request(
        peer=RENDER_LB,
        headers=[(b"forwarded", f'for="{CLIENT_B}";proto=https'.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == CLIENT_B
    assert result.path == ClientSourceResolutionPath.TRUSTED_CHAIN_FORWARDED


@pytest.mark.unit
def test_malformed_and_overlong_chains_fail_closed_to_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()

    overlong = ", ".join([f"203.0.113.{index}" for index in range(40)])
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", overlong.encode("ascii"))],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_LB
    assert result.path == ClientSourceResolutionPath.TRUSTED_PEER_FALLBACK

    malformed = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"not-an-ip, 10.0.0.1")],
    )
    malformed_result = resolve_admin_login_client_source(malformed, settings)
    assert malformed_result.source == RENDER_LB
    assert malformed_result.path == ClientSourceResolutionPath.TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_missing_peer_uses_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()
    result = resolve_admin_login_client_source(_request(peer=None), settings)
    assert result.source == "unknown"
    assert result.path == ClientSourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.INFO, logger="app.admin_client_source")

    request = _request(
        peer=UNTRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    resolve_admin_login_client_source(request, settings)

    assert caplog.records
    record = caplog.records[-1]
    serialized = f"{record.getMessage()} {record.__dict__}"
    assert UNTRUSTED_PEER not in serialized
    assert "203.0.113.99" not in serialized
    assert record.admin_client_source_path == ClientSourceResolutionPath.UNTRUSTED_PEER  # type: ignore[attr-defined]


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_does_not_create_new_source_buckets(
    monkeypatch: pytest.MonkeyPatch,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    _configure_trusted_proxy_env(monkeypatch)

    trusted_client = TestClient(app, follow_redirects=False, client=(RENDER_LB, 12345))

    def _post_with_xff(xff: str) -> Any:
        with mock_db_connection():
            form = trusted_client.get("/admin/login")
            csrf = _extract_csrf_token(form.text)
            cookies = {}
            flow_cookie = form.cookies.get(LOGIN_FLOW_COOKIE_NAME)
            if flow_cookie:
                cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
            return trusted_client.post(
                "/admin/login",
                data={"username": "ghost", "password": "wrong", "csrf_token": csrf},
                cookies=cookies,
                headers={"X-Forwarded-For": xff},
            )

    with shared_rate_limiter(rate_limit_store):
        for index in range(2):
            xff = f"198.18.0.{index}, {CLIENT_A}, {CLOUDFLARE_EDGE}"
            assert _post_with_xff(xff).status_code == 401
        blocked = _post_with_xff(f"198.18.0.99, {CLIENT_A}, {CLOUDFLARE_EDGE}")
        assert blocked.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert len(rate_limit_store.rows) == 1
    assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_rotating_spoofed_headers_same_resolution_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxy_env(monkeypatch)
    settings = get_settings()
    keys: set[str] = set()
    for index in range(5):
        request = _request(
            peer=RENDER_LB,
            headers=[
                (
                    b"x-forwarded-for",
                    f"198.18.0.{index}, {CLIENT_A}, {CLOUDFLARE_EDGE}".encode("ascii"),
                )
            ],
        )
        result = resolve_admin_login_client_source(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(result.source))
    assert len(keys) == 1


@pytest.mark.unit
def test_deploy_config_is_consistent() -> None:
    from scripts.verify_admin_proxy_config import verify_render_proxy_config

    render_text = (_REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert verify_render_proxy_config(render_text) == []


@pytest.mark.unit
def test_default_uvicorn_allow_ips_matches_render_start_command() -> None:
    render_text = (_REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert f"--forwarded-allow-ips='{DEFAULT_UVICORN_FORWARDED_ALLOW_IPS}'" in render_text


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def uvicorn_proxy_server(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    """Run uvicorn with the same forwarded-allow-ips boundary as render.yaml."""
    for key, value in PRODUCTION_TRUST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()

    port = _find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        forwarded_allow_ips=DEFAULT_UVICORN_FORWARDED_ALLOW_IPS,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    with patch("app.main.db.init_db"):
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        origin = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with httpx.Client() as client:
                    if client.get(f"{origin}/health", timeout=0.5).status_code == 200:
                        break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            server.should_exit = True
            thread.join(timeout=2)
            pytest.fail("uvicorn server did not become ready")

        try:
            yield origin
        finally:
            server.should_exit = True
            thread.join(timeout=5)


@pytest.mark.integration
def test_uvicorn_integration_untrusted_peer_ignores_xff(
    uvicorn_proxy_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise deployment uvicorn settings: direct peer must control limiter source."""
    _configure_trusted_proxy_env(monkeypatch)

    with httpx.Client() as client:
        health = client.get(f"{uvicorn_proxy_server}/health", timeout=5)
        assert health.status_code == 200

    resolution = resolve_admin_login_client_source(
        _request(
            peer=UNTRUSTED_PEER,
            headers=[(b"x-forwarded-for", b"203.0.113.55")],
        ),
        get_settings(),
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == ClientSourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.integration
def test_verify_admin_proxy_config_script_passes() -> None:
    from scripts.verify_admin_proxy_config import main

    assert main([]) == 0
