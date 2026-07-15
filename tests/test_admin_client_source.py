"""Tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    MISSING_CLIENT_SOURCE,
    normalize_ip_address,
    parse_trusted_networks,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PRIVATE_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"
TEST_CLOUDFLARE_CIDRS = "103.21.244.0/22,2400:cb00::/32"


def _settings(
    *,
    trust_proxy: bool = False,
    trusted_cidrs: str = "",
    cloudflare_cidrs: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="test-session-secret-32chars-minimum",
        admin_trust_proxy_headers=trust_proxy,
        admin_trusted_proxy_cidrs=trusted_cidrs,
        admin_cloudflare_proxy_cidrs=cloudflare_cidrs,
    )


def _request(
    *,
    peer: str,
    headers: dict[str, str] | None = None,
) -> Request:
    header_items = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_items,
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_ip_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None
    assert normalize_ip_address("999.999.999.999") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored() -> None:
    settings = _settings(trust_proxy=False)
    for header_value in ("203.0.113.99", "203.0.113.99, 198.51.100.10"):
        request = _request(
            peer="198.51.100.10",
            headers={"X-Forwarded-For": header_value},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost_value() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
    )
    request = _request(
        peer="10.0.0.2",
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "trusted_chain_x_forwarded_for"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=f"{RENDER_PRIVATE_CIDRS},{TEST_CLOUDFLARE_CIDRS}",
        cloudflare_cidrs=TEST_CLOUDFLARE_CIDRS,
    )
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.50, 103.21.244.10, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path == "trusted_chain_x_forwarded_for"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_ignores_forwarded_chain() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
    )
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.5"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
        cloudflare_cidrs=TEST_CLOUDFLARE_CIDRS,
    )
    request = _request(
        peer="198.51.100.10",
        headers={
            "CF-Connecting-IP": "203.0.113.77",
            "X-Forwarded-For": "203.0.113.77",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == "direct_peer"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_present() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=f"{RENDER_PRIVATE_CIDRS},{TEST_CLOUDFLARE_CIDRS}",
        cloudflare_cidrs=TEST_CLOUDFLARE_CIDRS,
    )
    request = _request(
        peer="10.0.0.5",
        headers={
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.99, 103.21.244.10, 10.0.0.5",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.88"
    assert resolution.path == "trusted_cf_connecting_ip_x_forwarded_for"


@pytest.mark.unit
def test_forwarded_header_precedence_over_x_forwarded_for() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
    )
    request = _request(
        peer="10.0.0.5",
        headers={
            "Forwarded": 'for=203.0.113.60;proto=https, for="10.0.0.5"',
            "X-Forwarded-For": "203.0.113.99, 10.0.0.5",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path == "trusted_chain_forwarded"


@pytest.mark.unit
def test_single_hop_forwarding_is_ambiguous() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
    )
    request = _request(
        peer="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == MISSING_CLIENT_SOURCE
    assert resolution.path == "single_hop_forwarding_ambiguous"


@pytest.mark.unit
def test_overlong_forwarding_chain_is_conservative() -> None:
    settings = _settings(
        trust_proxy=True,
        trusted_cidrs=RENDER_PRIVATE_CIDRS,
    )
    hops = ", ".join(f"203.0.113.{index}" for index in range(1, 25))
    request = _request(peer="10.0.0.5", headers={"X-Forwarded-For": hops})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == MISSING_CLIENT_SOURCE
    assert resolution.path == "overlong_forwarding_chain"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(trust_proxy=False)
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request(
                    peer="198.51.100.10",
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                ),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_telemetry_and_limiter_state_contain_no_raw_forwarding_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(trust_proxy=False)
    request = _request(
        peer="198.51.100.10",
        headers={
            "X-Forwarded-For": "203.0.113.99, 198.51.100.10",
            "CF-Connecting-IP": "203.0.113.99",
        },
    )
    with caplog.at_level(logging.DEBUG):
        resolution = resolve_admin_login_client_source(request, settings)

    limiter_key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert "203.0.113.99" not in limiter_key
    assert "198.51.100.10" not in limiter_key
    assert len(limiter_key) == 64
    for record in caplog.records:
        rendered = record.getMessage()
        if record.exc_info:
            rendered += str(record.exc_info)
        assert "203.0.113.99" not in rendered
        assert "X-Forwarded-For" not in rendered
        assert "CF-Connecting-IP" not in rendered


@pytest.mark.unit
def test_parse_trusted_networks_accepts_hosts_and_cidrs() -> None:
    networks = parse_trusted_networks("10.0.0.0/8,127.0.0.1,::1/128")
    assert len(networks) == 3


@pytest.mark.integration
def test_uvicorn_proxy_chain_matches_application_trust_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    monkeypatch.delenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--forwarded-allow-ips",
        "127.0.0.0/8",
        "--log-level",
        "warning",
    ]
    env = os.environ.copy()
    env["BASE_URL"] = f"http://127.0.0.1:{port}"
    env.pop("DATABASE_URL", None)
    env.pop("ADMIN_USERNAME", None)
    env.pop("ADMIN_PASSWORD_HASH", None)
    env.pop("ADMIN_SESSION_SECRET", None)
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        settings = get_settings()
        scope_request = _request(
            peer="127.0.0.1",
            headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
        )
        resolution = resolve_admin_login_client_source(scope_request, settings)
        assert resolution.source == "198.51.100.10"

        response = httpx.get(
            f"http://127.0.0.1:{port}/admin/login",
            headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
            timeout=5.0,
        )
        assert response.status_code in {200, 503}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.unit
def test_limiter_admissions_do_not_multiply_for_rotated_spoof_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration with TestClient limiter path using untrusted direct peer."""
    from argon2 import PasswordHasher

    from tests.test_admin_auth import FakeRateLimitStore, mock_db_connection, shared_rate_limiter

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PasswordHasher().hash("pw"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)

    store = FakeRateLimitStore()
    client = TestClient(app, follow_redirects=False)
    with shared_rate_limiter(store):
        with mock_db_connection():
            for index in range(4):
                login_page = client.get("/admin/login")
                csrf = login_page.text.split('name="csrf_token" value="')[1].split('"')[0]
                response = client.post(
                    "/admin/login",
                    data={
                        "username": f"user-{index}",
                        "password": "wrong",
                        "csrf_token": csrf,
                    },
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                )
                if index < 3:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 429
    assert len(store.rows) == 1
