"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_client_source import (
    DEFAULT_RENDER_TRUSTED_PROXY_NETWORKS,
    ClientSourceResolution,
    deployment_proxy_trust_summary,
    normalize_ip_address,
    parse_trusted_networks,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import Settings, get_settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_render_yaml_text() -> str:
    return (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")


def _render_env_value(text: str, key: str) -> str:
    match = re.search(rf"- key: {re.escape(key)}\n\s+value: \"([^\"]*)\"", text)
    assert match is not None, key
    return match.group(1)


RENDER_LB = "10.0.0.1"
ATTACKER = "198.51.100.10"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "203.0.113.88"
TRUSTED_NETWORKS = "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
CF_EDGE = "173.245.48.1"
CF_NETWORKS = "173.245.48.0/20"


def _settings(
    *,
    trusted: str = TRUSTED_NETWORKS,
    cloudflare: str = "",
    legacy_trust_flag: bool = False,
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
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
        admin_username="operator",
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_trusted_proxy_networks=trusted,
        admin_cloudflare_proxy_networks=cloudflare,
        admin_trust_proxy_headers=legacy_trust_flag,
    )


def _request(
    *,
    peer: str | None = ATTACKER,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": None if peer is None else (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _resolve(
    request: Request,
    settings: Settings | None = None,
) -> ClientSourceResolution:
    return resolve_admin_login_client_source(request, settings or _settings())


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.mark.unit
def test_normalize_ipv4_ipv6_and_mapped() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("203.0.113.1:8080") == "203.0.113.1"
    assert normalize_ip_address("  ") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored() -> None:
    settings = _settings()
    for header_value in ("203.0.113.99", "203.0.113.99, 10.0.0.1"):
        request = _request(
            peer=ATTACKER,
            headers=[(b"x-forwarded-for", header_value.encode())],
        )
        result = _resolve(request, settings)
        assert result.source == ATTACKER
        assert result.path == "direct_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_leftmost_spoof() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"203.0.113.1, {REAL_CLIENT}".encode())],
    )
    result = _resolve(request)
    assert result.source == REAL_CLIENT
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"{REAL_CLIENT}, 203.0.113.60, {RENDER_LB}".encode())],
    )
    result = _resolve(request)
    assert result.source == REAL_CLIENT
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_partial_trust_single_hop_forwarding_fails_closed() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"203.0.113.50")],
    )
    result = _resolve(request)
    assert result.source == RENDER_LB
    assert result.path == "trusted_peer_fallback"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip() -> None:
    request = _request(
        peer=ATTACKER,
        headers=[
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
            (b"cf-ray", b"abc123"),
        ],
    )
    result = _resolve(request)
    assert result.source == ATTACKER
    assert result.path == "direct_peer"


@pytest.mark.unit
def test_cf_connecting_ip_only_when_peer_is_cloudflare_edge() -> None:
    settings = _settings(cloudflare=CF_NETWORKS)
    request = _request(
        peer=CF_EDGE,
        headers=[(b"cf-connecting-ip", REAL_CLIENT.encode())],
    )
    result = _resolve(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == "cf_connecting_ip_verified"


@pytest.mark.unit
def test_cf_connecting_ip_rejected_at_render_hop() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
            (b"x-forwarded-for", f"203.0.113.1, {REAL_CLIENT}".encode()),
        ],
    )
    result = _resolve(request)
    assert result.source == REAL_CLIENT
    assert result.path == "xff_trusted_chain"


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", f"203.0.113.1, {REAL_CLIENT}".encode()),
            (b"forwarded", b'for="203.0.113.1";proto=https'),
            (b"cf-connecting-ip", b"203.0.113.1"),
        ],
    )
    result = _resolve(request)
    assert result.source == REAL_CLIENT


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing() -> None:
    request = _request(
        peer=RENDER_LB,
        headers=[
            (
                b"forwarded",
                f'for="{REAL_CLIENT}";for="203.0.113.60";for="{RENDER_LB}"'.encode(),
            )
        ],
    )
    result = _resolve(request)
    assert result.source == REAL_CLIENT
    assert result.path == "forwarded_trusted_chain"


@pytest.mark.unit
def test_address_format_edge_cases() -> None:
    settings = _settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b" , , 203.0.113.1 , 203.0.113.2 ")],
    )
    assert _resolve(request).source == "203.0.113.2"

    overlong = ", ".join([f"203.0.113.{index}" for index in range(1, 40)])
    overlong_request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", overlong.encode())],
    )
    assert _resolve(overlong_request).path == "trusted_peer_fallback"

    invalid = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"not-an-ip, 10.0.0.1")],
    )
    assert _resolve(invalid).path == "trusted_peer_fallback"

    missing_peer = _request(peer=None, headers=[(b"x-forwarded-for", REAL_CLIENT.encode())])
    assert _resolve(missing_peer).source == "unknown"


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_without_networks_fails_closed() -> None:
    settings = _settings(trusted="", legacy_trust_flag=True)
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", f"203.0.113.1, {REAL_CLIENT}".encode())],
    )
    result = _resolve(request, settings)
    assert result.source == RENDER_LB
    assert result.path == "direct_peer"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_NETWORKS", TRUSTED_NETWORKS)
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            admin_auth.client_ip(
                _request(
                    peer=ATTACKER,
                    headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode())],
                ),
                settings,
            )
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_source_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_NETWORKS", TRUSTED_NETWORKS)
    settings = get_settings()
    source = admin_auth.client_ip(
        _request(
            peer=ATTACKER,
            headers=[(b"x-forwarded-for", b"203.0.113.99, 203.0.113.100")],
        ),
        settings,
    )
    key = admin_auth.build_source_rate_limit_key(source)
    assert source == ATTACKER
    assert "203.0.113" not in key
    assert len(key) == 64


@pytest.mark.unit
def test_audit_and_logs_exclude_raw_forwarding_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", b"203.0.113.50, 203.0.113.60"),
            (b"forwarded", b'for="203.0.113.50"'),
            (b"cf-connecting-ip", b"203.0.113.50"),
        ],
    )
    _resolve(request)
    blob = " ".join(
        record.message + str(getattr(record, "resolution_path", "")) for record in caplog.records
    )
    assert "203.0.113.50" not in blob
    assert "x-forwarded-for" not in blob.lower()


@pytest.mark.unit
def test_deployment_proxy_trust_summary_has_no_raw_ips() -> None:
    summary = deployment_proxy_trust_summary(_settings())
    encoded = json.dumps(summary)
    assert "10.0.0.0" not in encoded
    assert summary["trusted_networks_configured"] is True


@pytest.mark.unit
def test_health_reports_admin_source_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_NETWORKS", TRUSTED_NETWORKS)
    monkeypatch.setenv("DATABASE_URL", "")
    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_source_trust"]["trusted_networks_configured"] is True
    assert payload["admin_source_trust"]["resolution"] == "trusted_hop_parser"


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    text = _read_render_yaml_text()
    start_command = text.split("startCommand:", 1)[1].split("healthCheckPath:", 1)[0]
    assert "--proxy-headers" in start_command
    assert "--forwarded-allow-ips" in start_command

    trusted = _render_env_value(text, "ADMIN_TRUSTED_PROXY_NETWORKS")
    assert "10.0.0.0/8" in trusted
    assert "172.16.0.0/12" in trusted
    assert "192.168.0.0/16" in trusted

    allow_ips_match = re.search(r"--forwarded-allow-ips='([^']+)'", start_command)
    assert allow_ips_match is not None
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert network in allow_ips_match.group(1)


@pytest.mark.unit
def test_default_render_trusted_networks_match_render_yaml() -> None:
    text = _read_render_yaml_text()
    env_value = _render_env_value(text, "ADMIN_TRUSTED_PROXY_NETWORKS")
    assert parse_trusted_networks(env_value) == parse_trusted_networks(
        DEFAULT_RENDER_TRUSTED_PROXY_NETWORKS
    )


@pytest.mark.integration
def test_uvicorn_proxy_configuration_matches_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise admin login through uvicorn with deployment proxy flags."""
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("integration-test-password"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_NETWORKS", TRUSTED_NETWORKS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")

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
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1,10.0.0.0/8",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(f"uvicorn did not become ready: {last_error}")

        health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
        assert health.json()["admin_source_trust"]["trusted_networks_configured"] is True

        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.side_effect = RuntimeError("db unavailable for integration probe")
            spoofed = httpx.post(
                f"http://127.0.0.1:{port}/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": "token",
                },
                headers={
                    "X-Forwarded-For": f"203.0.113.1, {REAL_CLIENT}",
                    "X-Real-IP": "203.0.113.1",
                },
                timeout=5.0,
            )
        assert spoofed.status_code in {400, 401, 429, 503}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
