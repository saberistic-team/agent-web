"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

pytest_plugins = ["tests.test_admin_auth"]

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    TrustedProxyBoundary,
    normalize_client_address,
    parse_trusted_proxy_cidrs,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_USERNAME,
    _extract_csrf_token,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TRUSTED_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32,::1/128"
RENDER_TRUSTED_PROXY_CIDRS = TRUSTED_PROXY_CIDRS


def _settings(*, trusted_proxy_cidrs: str = "") -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="ops@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="test",
        admin_username=TEST_USERNAME,
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_trusted_proxy_cidrs=parse_trusted_proxy_cidrs(trusted_proxy_cidrs),
    )


def _request(
    *,
    peer: str | None = "203.0.113.10",
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin1"), value.encode("latin1"))
            for key, value in (headers or {}).items()
        ],
        "method": "POST",
        "path": "/admin/login",
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_client_address("  203.0.113.2  ") == "203.0.113.2"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("x" * 300) is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored() -> None:
    settings = _settings()
    for header_value in ("203.0.113.99", "203.0.113.99, 10.0.0.1"):
        request = _request(
            peer="198.51.100.10",
            headers={"X-Forwarded-For": header_value},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.44"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.44"
    assert resolution.path is ClientSourceResolutionPath.X_FORWARDED_FOR_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path is ClientSourceResolutionPath.X_FORWARDED_FOR_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="203.0.113.200",
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.200"
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="203.0.113.55",
        headers={
            "CF-Connecting-IP": "203.0.113.99",
            "True-Client-IP": "203.0.113.88",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.55"
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_headers() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="10.0.0.9",
        headers={
            "CF-Connecting-IP": "203.0.113.41",
            "X-Forwarded-For": "203.0.113.99, 10.0.0.9",
            "Forwarded": 'for=203.0.113.55;proto=https, for=10.0.0.9',
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.41"
    assert resolution.path is ClientSourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_cf_header_missing() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="10.0.0.9",
        headers={
            "Forwarded": 'for=203.0.113.60;proto=https, for="[2001:db8::9]";proto=https',
            "X-Forwarded-For": "203.0.113.99, 10.0.0.9",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "2001:db8::9"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
def test_address_format_edge_cases_are_conservative() -> None:
    settings = _settings(trusted_proxy_cidrs=TRUSTED_PROXY_CIDRS)
    overlong = ", ".join(f"10.0.0.{index}" for index in range(40))
    request = _request(peer="10.0.0.2", headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.2"
    assert resolution.path is ClientSourceResolutionPath.INVALID_FORWARDING_FALLBACK_PEER

    malformed = _request(peer="10.0.0.2", headers={"X-Forwarded-For": " , , "})
    malformed_resolution = resolve_admin_login_client_source(malformed, settings)
    assert malformed_resolution.source == "10.0.0.2"
    assert malformed_resolution.path is ClientSourceResolutionPath.INVALID_FORWARDING_FALLBACK_PEER


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_do_not_create_new_limiter_rows(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    with shared_rate_limiter(rate_limit_store):
        for index in range(5):
            response = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            assert response.status_code == 401

        blocked = _login(
            username="ghost",
            password="wrong",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert blocked.status_code == 429
        assert len(rate_limit_store.rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_trusted_proxy_limiter_uses_real_client_not_spoofed_leftmost(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    from starlette.testclient import TestClient

    from app.main import app

    proxy_client = TestClient(app, follow_redirects=False, client=("10.0.0.3", 51515))
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            for spoofed in ("203.0.113.99", "203.0.113.55"):
                form = proxy_client.get("/admin/login")
                csrf_token = _extract_csrf_token(form.text)
                response = proxy_client.post(
                    "/admin/login",
                    data={
                        "username": "ghost",
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    headers={"X-Forwarded-For": f"{spoofed}, 203.0.113.70"},
                )
                assert response.status_code == 401

            form = proxy_client.get("/admin/login")
            csrf_token = _extract_csrf_token(form.text)
            blocked = proxy_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                headers={"X-Forwarded-For": "203.0.113.88, 203.0.113.70"},
            )
            assert blocked.status_code == 429
            assert len(rate_limit_store.rows) == 1
            source_key = admin_auth.build_source_rate_limit_key("203.0.113.70")
            assert source_key in rate_limit_store.rows


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_rows_exclude_raw_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    request = _request(
        peer="203.0.113.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    settings = get_settings()
    with caplog.at_level(logging.INFO):
        source = admin_auth.client_ip(request, settings)
    assert source == "203.0.113.10"
    assert "203.0.113.99" not in caplog.text
    assert "x-forwarded-for" not in caplog.text.lower()
    key = admin_auth.build_source_rate_limit_key(source)
    assert "203.0.113" not in key
    assert len(key) == 64


@pytest.mark.unit
def test_render_yaml_proxy_trust_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert RENDER_TRUSTED_PROXY_CIDRS in render_yaml
    assert "--forwarded-allow-ips=" in render_yaml
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert cidr in render_yaml


@pytest.mark.unit
def test_health_reports_admin_client_source_trust_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    from starlette.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_client_source_trust"]["configured"] is True
    assert payload["admin_client_source_trust"]["trusted_proxy_cidr_count"] >= 5


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_env_maps_to_private_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    assert "10.0.0.0/8" in settings.admin_trusted_proxy_cidrs


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_proxy_headers_middleware_matches_deployment_boundary() -> None:
    """Exercise the same ProxyHeadersMiddleware Uvicorn enables in production."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    captured: dict[str, str] = {}

    async def _capture_scope(scope, receive, send):  # type: ignore[no-untyped-def]
        captured["client"] = scope["client"][0]

    wrapped = ProxyHeadersMiddleware(_capture_scope, trusted_hosts=TRUSTED_PROXY_CIDRS)

    async def _run() -> None:
        scope = {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.99, 203.0.113.70"),
            ],
            "client": ("10.0.0.4", 9000),
            "method": "GET",
            "path": "/health",
        }

        async def receive():  # type: ignore[no-untyped-def]
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):  # type: ignore[no-untyped-def]
            return None

        await wrapped(scope, receive, send)

    import asyncio

    asyncio.run(_run())
    assert captured["client"] == "203.0.113.70"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_live_uvicorn_start_command_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke the render.yaml start command with explicit forwarded-allow-ips."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TRUSTED_PROXY_CIDRS)
    port = _find_free_port()

    env = os.environ.copy()
    env["ADMIN_TRUSTED_PROXY_CIDRS"] = TRUSTED_PROXY_CIDRS
    env.pop("DATABASE_URL", None)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--forwarded-allow-ips",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1",
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 30
        last_error = ""
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    f"uvicorn exited early ({process.returncode}): {stderr or stdout}"
                )
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            except (httpx.HTTPError, OSError) as exc:
                last_error = str(exc)
                time.sleep(0.25)
                continue
            if response.status_code == 200:
                payload = response.json()
                assert payload["status"] == "ok"
                assert payload["admin_client_source_trust"]["configured"] is True
                return
            last_error = f"status={response.status_code}"
            time.sleep(0.25)
        raise AssertionError(f"uvicorn did not become ready: {last_error}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.unit
def test_trusted_proxy_boundary_supports_literals_for_tests() -> None:
    boundary = TrustedProxyBoundary(("testclient", "10.0.0.0/8"))
    assert "testclient" in boundary
    assert "10.0.0.1" in boundary
    assert "203.0.113.1" not in boundary
