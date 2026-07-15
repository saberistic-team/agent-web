"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import patch

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_address,
    parse_trusted_proxy_networks,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_PASSWORD,
    TEST_USERNAME,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
RENDER_LB_PEER = "10.0.0.55"
CLOUDFLARE_CLIENT = "203.0.113.77"
ATTACKER_PEER = "198.51.100.10"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)


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
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    admin_auth.reset_login_rate_limiter()
    reset_client_source_telemetry_for_tests()


class PeerOverrideMiddleware:
    """ASGI middleware that sets the immediate TCP peer for proxy-trust tests."""

    def __init__(self, asgi_app: Any, peer_host: str) -> None:
        self.app = asgi_app
        self.peer_host = peer_host

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (self.peer_host, 12345)}
        await self.app(scope, receive, send)


def _request_with_peer(
    peer: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_with_trusted_proxies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted: str = PRODUCTION_TRUSTED_PROXIES,
) -> Any:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    if trusted:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted)
        monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", trusted)
    else:
        monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
        monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    return get_settings()


def _resolve(
    peer: str,
    settings: Any,
    *,
    headers: dict[str, str] | None = None,
) -> ClientSourceResolution:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    request = _request_with_peer(peer, headers=header_list)
    return resolve_admin_login_client_source(request, settings)


@pytest.fixture
def trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    return _settings_with_trusted_proxies(monkeypatch)


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("203.0.113.1:abc") is None


@pytest.mark.unit
def test_parse_trusted_proxy_networks_accepts_hosts_and_cidrs() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8,203.0.113.1,2001:db8::/32")
    assert len(networks) == 3


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    single = _resolve(
        ATTACKER_PEER,
        settings,
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    multi = _resolve(
        ATTACKER_PEER,
        settings,
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    assert single == ClientSourceResolution(ATTACKER_PEER, "direct_peer")
    assert multi == ClientSourceResolution(ATTACKER_PEER, "direct_peer")


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    trusted_proxy_env: Any,
) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={
            "X-Forwarded-For": f"203.0.113.1, {CLOUDFLARE_CLIENT}",
            "CF-Connecting-IP": CLOUDFLARE_CLIENT,
        },
    )
    assert resolution.source == CLOUDFLARE_CLIENT
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(trusted_proxy_env: Any) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={"X-Forwarded-For": f"{CLOUDFLARE_CLIENT}, {RENDER_LB_PEER}"},
    )
    assert resolution.source == CLOUDFLARE_CLIENT
    assert resolution.path == "trusted_xff_hops"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    trusted_proxy_env: Any,
) -> None:
    untrusted_proxy = "203.0.113.200"
    resolution = _resolve(
        untrusted_proxy,
        trusted_proxy_env,
        headers={
            "X-Forwarded-For": f"{CLOUDFLARE_CLIENT}, {RENDER_LB_PEER}",
            "CF-Connecting-IP": CLOUDFLARE_CLIENT,
        },
    )
    assert resolution.source == untrusted_proxy
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    resolution = _resolve(
        ATTACKER_PEER,
        settings,
        headers={
            "CF-Connecting-IP": CLOUDFLARE_CLIENT,
            "X-Forwarded-For": CLOUDFLARE_CLIENT,
        },
    )
    assert resolution.source == ATTACKER_PEER
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_forwarded_and_xff(
    trusted_proxy_env: Any,
) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={
            "CF-Connecting-IP": CLOUDFLARE_CLIENT,
            "Forwarded": "for=203.0.113.10;proto=https",
            "X-Forwarded-For": "203.0.113.20, 203.0.113.30",
        },
    )
    assert resolution.source == CLOUDFLARE_CLIENT
    assert resolution.path == "trusted_cf_connecting_ip"


@pytest.mark.unit
def test_header_precedence_forwarded_before_xff_without_cf(
    trusted_proxy_env: Any,
) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={
            "Forwarded": 'for="203.0.113.44";proto=https',
            "X-Forwarded-For": "203.0.113.55",
        },
    )
    assert resolution.source == "203.0.113.44"
    assert resolution.path == "trusted_forwarded"


@pytest.mark.unit
def test_xff_right_to_left_skips_trusted_hops(trusted_proxy_env: Any) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={"X-Forwarded-For": f"203.0.113.90, {RENDER_LB_PEER}"},
    )
    assert resolution.source == "203.0.113.90"
    assert resolution.path == "trusted_xff_hops"


@pytest.mark.unit
def test_invalid_xff_chain_falls_back_to_peer(trusted_proxy_env: Any) -> None:
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    assert resolution.source == RENDER_LB_PEER
    assert resolution.path == "invalid_forwarding"


@pytest.mark.unit
def test_overlong_forwarding_header_falls_back_to_peer(trusted_proxy_env: Any) -> None:
    huge = "203.0.113.1, " * 500
    resolution = _resolve(
        RENDER_LB_PEER,
        trusted_proxy_env,
        headers={"X-Forwarded-For": huge},
    )
    assert resolution.source == RENDER_LB_PEER
    assert resolution.path == "invalid_forwarding"


@pytest.mark.unit
def test_missing_peer_resolves_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == "missing_peer"


@pytest.mark.unit
def test_rotating_spoofed_headers_do_not_create_new_source_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request_with_peer(
                    ATTACKER_PEER,
                    headers=[(b"x-forwarded-for", f"203.0.113.{i}".encode())],
                ),
                settings,
            ).source
        )
        for i in range(8)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_privacy_logs_and_limiter_rows_exclude_raw_forwarding_data(
    trusted_proxy_env: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    caplog.set_level(logging.INFO, logger="app.admin_auth")
    request = _request_with_peer(
        RENDER_LB_PEER,
        headers=[
            (b"x-forwarded-for", b"203.0.113.1, 203.0.113.2"),
            (b"cf-connecting-ip", CLOUDFLARE_CLIENT.encode()),
        ],
    )
    source = admin_auth.client_ip(request, trusted_proxy_env)
    source_key = admin_auth.build_source_rate_limit_key(source)
    combined = caplog.text + str(source_key)
    assert "203.0.113.1" not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert "cf-connecting-ip" not in combined.lower()
    assert len(source_key) == 64


@pytest.mark.unit
def test_untrusted_forwarding_telemetry_is_sampled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch, trusted="")
    reset_client_source_telemetry_for_tests()
    caplog.set_level(logging.INFO, logger="app.admin_client_source")
    request = _request_with_peer(
        ATTACKER_PEER,
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    for _ in range(3):
        resolve_admin_login_client_source(request, settings)
    assert caplog.text.count("Admin login ignored untrusted forwarding headers") == 1


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.mark.unit
def test_deployment_configuration_is_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "UVICORN_FORWARDED_ALLOW_IPS" in render_yaml
    assert PRODUCTION_TRUSTED_PROXIES in render_yaml
    assert "--forwarded-allow-ips" in render_yaml
    assert PRODUCTION_TRUSTED_PROXIES in render_yaml.split("--forwarded-allow-ips", 1)[1]

    admin_auth_doc = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in admin_auth_doc
    assert "CF-Connecting-IP" in admin_auth_doc
    assert "right-to-left" in admin_auth_doc.lower()


@pytest.mark.integration
def test_rate_limit_through_trusted_proxy_peer_middleware(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", PRODUCTION_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    proxy_app = PeerOverrideMiddleware(app, RENDER_LB_PEER)
    proxy_client = TestClient(proxy_app, follow_redirects=False)
    headers = {
        "CF-Connecting-IP": CLOUDFLARE_CLIENT,
        "X-Forwarded-For": f"203.0.113.1, {CLOUDFLARE_CLIENT}",
    }

    def _proxy_login(**kwargs: Any) -> Any:
        with mock_db_connection():
            form = proxy_client.get("/admin/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
            assert csrf is not None
            data = {
                "username": kwargs.get("username", TEST_USERNAME),
                "password": kwargs.get("password", TEST_PASSWORD),
                "csrf_token": csrf.group(1),
            }
            cookies = {}
            flow = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
            if flow:
                cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow
            return proxy_client.post(
                "/admin/login",
                data=data,
                cookies=cookies,
                headers=kwargs.get("headers", headers),
            )

    with shared_rate_limiter(rate_limit_store):
        assert _proxy_login(username="ghost", password="wrong").status_code == 401
        assert _proxy_login(username="ghost", password="wrong").status_code == 401
        blocked = _proxy_login(username="ghost", password="wrong")
        assert blocked.status_code == 429

        other_headers = {
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.1, 203.0.113.88",
        }
        assert _proxy_login(username="ghost", password="wrong", headers=other_headers).status_code == 401


@pytest.mark.integration
def test_rate_limit_rotating_spoofed_headers_single_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", PRODUCTION_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    proxy_app = PeerOverrideMiddleware(app, RENDER_LB_PEER)

    with shared_rate_limiter(rate_limit_store):
        for index in range(4):
            spoof_headers = {
                "CF-Connecting-IP": CLOUDFLARE_CLIENT,
                "X-Forwarded-For": f"203.0.113.{index}, {CLOUDFLARE_CLIENT}",
            }
            with mock_db_connection():
                client = TestClient(proxy_app, follow_redirects=False)
                form = client.get("/admin/login")
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
                assert csrf is not None
                cookies = {}
                flow = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong",
                        "csrf_token": csrf.group(1),
                    },
                    cookies=cookies,
                    headers=spoof_headers,
                )
            if index < 2:
                assert response.status_code == 401
            elif index == 2:
                assert response.status_code == 429


@pytest.mark.integration
def test_health_reports_admin_source_trust_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", PRODUCTION_TRUSTED_PROXIES)
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", PRODUCTION_TRUSTED_PROXIES)
    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_source_trust"]["trusted_proxies_configured"] is True
    assert payload["admin_source_trust"]["forwarded_allow_ips_configured"] is True


@contextmanager
def _uvicorn_server(
    *,
    host: str = "127.0.0.1",
    forwarded_allow_ips: str = "127.0.0.1",
    trusted_proxy_ips: str = "127.0.0.1",
) -> Generator[str, None, None]:
    """Start uvicorn with deployment-equivalent forwarded-header settings."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        bound_port = sock.getsockname()[1]

    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DATABASE_URL"
    }
    env.update(
        {
            "ADMIN_USERNAME": TEST_USERNAME,
            "ADMIN_PASSWORD_HASH": TEST_HASH,
            "ADMIN_SESSION_SECRET": "test-session-secret-32chars-minimum",
            "BASE_URL": f"http://{host}:{bound_port}",
            "ADMIN_TRUSTED_PROXY_IPS": trusted_proxy_ips,
            "UVICORN_FORWARDED_ALLOW_IPS": forwarded_allow_ips,
            "ADMIN_LOGIN_RATE_LIMIT": "3",
        }
    )
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(bound_port),
        "--forwarded-allow-ips",
        forwarded_allow_ips,
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    origin = f"http://{host}:{bound_port}"
    try:
        deadline = time.monotonic() + 15.0
        with httpx.Client() as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(f"{origin}/health", timeout=1.0).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("uvicorn did not become ready")
        yield origin
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.integration
def test_uvicorn_start_command_exposes_admin_source_trust() -> None:
    """Subprocess uses the same --forwarded-allow-ips flag declared in render.yaml."""
    with _uvicorn_server(forwarded_allow_ips="127.0.0.1", trusted_proxy_ips="127.0.0.1") as origin:
        with httpx.Client() as client:
            health = client.get(f"{origin}/health", headers={"X-Forwarded-For": "203.0.113.99"})
            assert health.status_code == 200
            assert health.json()["admin_source_trust"]["trusted_proxies_configured"] is True


@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_blocks_rotating_xff_spoof(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process uvicorn ProxyHeadersMiddleware plus app resolver (deployment stack)."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", PRODUCTION_TRUSTED_PROXIES)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    middleware_app = ProxyHeadersMiddleware(app, trusted_hosts=PRODUCTION_TRUSTED_PROXIES)
    proxy_app = PeerOverrideMiddleware(middleware_app, RENDER_LB_PEER)
    proxy_client = TestClient(proxy_app, follow_redirects=False)

    with shared_rate_limiter(rate_limit_store):
        for index in range(4):
            headers = {
                "CF-Connecting-IP": CLOUDFLARE_CLIENT,
                "X-Forwarded-For": f"203.0.113.{index}, {CLOUDFLARE_CLIENT}",
            }
            with mock_db_connection():
                form = proxy_client.get("/admin/login", headers=headers)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
                assert csrf is not None
                cookies = {}
                flow = form.cookies.get(admin_auth.LOGIN_FLOW_COOKIE_NAME)
                if flow:
                    cookies[admin_auth.LOGIN_FLOW_COOKIE_NAME] = flow
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong",
                        "csrf_token": csrf.group(1),
                    },
                    cookies=cookies,
                    headers=headers,
                )
            if index < 2:
                assert response.status_code == 401
            elif index == 2:
                assert response.status_code == 429
