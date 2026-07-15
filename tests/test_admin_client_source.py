"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import yaml
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app import admin_auth
from app.admin_client_source import (
    SourceResolutionPath,
    normalize_client_address,
    reset_admin_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.main import app
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    FakeRateLimitStore,
    _parse_login_form,
    mock_db_connection,
    rate_limit_store,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
RENDER_START_FORWARDED_ALLOW = "--forwarded-allow-ips 127.0.0.1"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
RENDER_START_FORWARDED_ALLOW = "--forwarded-allow-ips 127.0.0.1"


class _PeerOverrideMiddleware:
    """ASGI middleware that pins the immediate TCP peer for proxy-chain tests."""

    def __init__(self, inner_app: Any, peer_host: str) -> None:
        self._inner_app = inner_app
        self._peer_host = peer_host

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["client"] = (self._peer_host, 12345)
        await self._inner_app(scope, receive, send)


def _request_with_client(host: str, headers: dict[str, str] | None = None) -> Request:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_with_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    return get_settings()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.fixture(autouse=True)
def _reset_source_telemetry() -> None:
    reset_admin_client_source_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()

    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER

    request_multi = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    resolution_multi = resolve_admin_login_client_source(request_multi, settings)
    assert resolution_multi.source == "198.51.100.10"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.44"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.44"
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "198.51.100.10",
        headers={
            "CF-Connecting-IP": "203.0.113.77",
            "X-Forwarded-For": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.10",
            "Forwarded": 'for="203.0.113.20"',
            "CF-Connecting-IP": "203.0.113.30",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={"Forwarded": 'for="[2001:db8::5]";proto=https'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "2001:db8::5"
    assert resolution.path == SourceResolutionPath.TRUSTED_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_requires_xff_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={
            "X-Forwarded-For": "203.0.113.40",
            "CF-Connecting-IP": "203.0.113.40",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.40"
    assert resolution.path == SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1,oops", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_overlong_xff_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    hops = ", ".join(f"10.0.0.{i}" for i in range(40))
    request = _request_with_client("10.0.0.5", headers={"X-Forwarded-For": hops})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_only_trusted_xff_hops_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.CONSERVATIVE_FALLBACK


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    wrapped = _PeerOverrideMiddleware(app, "198.51.100.10")
    test_client = TestClient(wrapped, follow_redirects=False)

    def _local_login(headers: dict[str, str]) -> Any:
        with mock_db_connection():
            csrf_token, cookies = _parse_login_form(test_client.get("/admin/login"))
            return test_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers,
            )

    with shared_rate_limiter(rate_limit_store):
        for i in range(5):
            response = _local_login(headers={"X-Forwarded-For": f"203.0.113.{i}"})
            if i < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key("198.51.100.10")
    assert source_key in rate_limit_store.rows
    assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_limiter_uses_real_client_not_spoofed_leftmost(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    wrapped = _PeerOverrideMiddleware(app, "10.0.0.5")
    proxy_client = TestClient(wrapped, follow_redirects=False)

    def _proxy_login(**kwargs: Any) -> Any:
        headers = kwargs.pop("headers", None)
        with mock_db_connection():
            csrf_token, cookies = _parse_login_form(proxy_client.get("/admin/login"))
            data = {
                "username": kwargs.get("username", TEST_USERNAME),
                "password": kwargs.get("password", TEST_PASSWORD),
                "csrf_token": csrf_token,
            }
            return proxy_client.post(
                "/admin/login",
                data=data,
                cookies=cookies,
                headers=headers or {},
            )

    with shared_rate_limiter(rate_limit_store):
        spoof_headers = {"X-Forwarded-For": "203.0.113.1, 198.51.100.44"}
        assert _proxy_login(username="ghost", password="wrong", headers=spoof_headers).status_code == 401
        assert _proxy_login(username="ghost", password="wrong", headers=spoof_headers).status_code == 401
        blocked = _proxy_login(username="ghost", password="wrong", headers=spoof_headers)
        assert blocked.status_code == 429

        other_spoof = {"X-Forwarded-For": "203.0.113.99, 198.51.100.44"}
        assert _proxy_login(username="ghost", password="wrong", headers=other_spoof).status_code == 429

    assert admin_auth.build_source_rate_limit_key("198.51.100.44") in rate_limit_store.rows


@pytest.mark.unit
def test_render_deploy_proxy_settings_are_consistent() -> None:
    render_yaml = REPO_ROOT / "render.yaml"
    payload = yaml.safe_load(render_yaml.read_text(encoding="utf-8"))
    service = payload["services"][0]
    start_command = service["startCommand"]
    env_map = {item["key"]: item.get("value") for item in service["envVars"]}

    assert RENDER_START_FORWARDED_ALLOW in start_command
    assert env_map["ADMIN_TRUSTED_PROXY_CIDRS"] == RENDER_TRUSTED_CIDRS
    assert "ADMIN_TRUST_PROXY_HEADERS" not in env_map


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_rows_exclude_raw_forwarding(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)
    caplog.set_level(logging.DEBUG, logger="app.admin_client_source")

    request = _request_with_client(
        "10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.55, 198.51.100.99"},
    )
    settings = get_settings()
    resolve_admin_login_client_source(request, settings)

    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.55" not in message
        assert "198.51.100.99" not in message
        assert "x-forwarded-for" not in message.lower()
        if hasattr(record, "resolution_path"):
            assert "203.0.113" not in str(record.resolution_path)

    with shared_rate_limiter(rate_limit_store):
        wrapped = _PeerOverrideMiddleware(app, "10.0.0.5")
        proxy_client = TestClient(wrapped, follow_redirects=False)
        with mock_db_connection():
            csrf_token, cookies = _parse_login_form(proxy_client.get("/admin/login"))
            proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers={"X-Forwarded-For": "203.0.113.55, 198.51.100.99"},
            )

    for row in rate_limit_store.rows.values():
        row_text = repr(row)
        assert "203.0.113" not in row_text
        assert "forwarded" not in row_text.lower()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_chain_matches_deployment_settings(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise uvicorn --forwarded-allow-ips with the app resolver (not helper-only)."""
    port = _free_port()
    trusted_for_local = f"{RENDER_TRUSTED_CIDRS},127.0.0.1"
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_for_local)
    monkeypatch.setenv("BASE_URL", f"http://127.0.0.1:{port}")

    with (
        patch("app.main.db.init_db"),
        shared_rate_limiter(rate_limit_store),
    ):
        config = Config(
            app=app,
            host="127.0.0.1",
            port=port,
            forwarded_allow_ips="127.0.0.1",
            log_level="warning",
        )
        server = Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    health = httpx.get(f"{base}/health", timeout=1.0)
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:
                raise AssertionError("uvicorn did not become ready")

            with httpx.Client(base_url=base, timeout=5.0) as http_client:
                def _post_login(xff: str) -> httpx.Response:
                    with mock_db_connection():
                        login_page = http_client.get("/admin/login")
                        csrf_token = _extract_csrf_from_html(login_page.text)
                        flow_cookie = login_page.cookies.get("admin_login_flow", "")
                        return http_client.post(
                            "/admin/login",
                            data={
                                "username": "ghost",
                                "password": "wrong",
                                "csrf_token": csrf_token,
                            },
                            cookies={"admin_login_flow": flow_cookie},
                            headers={"X-Forwarded-For": xff},
                        )

                assert _post_login("203.0.113.1, 198.51.100.44").status_code == 401
                assert _post_login("203.0.113.2, 198.51.100.44").status_code == 401
                blocked = _post_login("203.0.113.3, 198.51.100.44")
                assert blocked.status_code == 429
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def _extract_csrf_from_html(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)
