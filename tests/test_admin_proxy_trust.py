"""Trusted-proxy client source resolution for admin login rate limiting (#239)."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.testclient import TestClient

from app import admin_auth
from app.config import get_settings
from app.main import app
from app.ip_networks import parse_trusted_proxy_networks
from app.proxy_trust import (
    SourceResolutionPath,
    normalize_client_address,
    resolve_admin_login_client_source,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

TRUSTED_PEER = "10.0.0.1"
RENDER_TRUSTED_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"
CF_EDGE = "172.64.0.1"
REAL_CLIENT = "203.0.113.55"
SPOOFED_CLIENT = "203.0.113.1"
UNTRUSTED_PEER = "198.51.100.10"


def _request_with_client(host: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings_with_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address(" 203.0.113.1 ") == "203.0.113.1"
    assert normalize_client_address("[2001:db8::1]") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.9") == "203.0.113.9"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_untrusted_peer_ignores_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    headers = [
        (b"x-forwarded-for", b"203.0.113.99"),
        (b"cf-connecting-ip", b"203.0.113.88"),
        (b"forwarded", b'for="203.0.113.77"'),
    ]
    request = _request_with_client(UNTRUSTED_PEER, headers)
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    headers = [(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")]
    request = _request_with_client(UNTRUSTED_PEER, headers)
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    chain = f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CF_EDGE}"
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"x-forwarded-for", chain.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    chain = f"{REAL_CLIENT}, {CF_EDGE}"
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"x-forwarded-for", chain.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    chain = f"{REAL_CLIENT}, {UNTRUSTED_PEER}"
    request = _request_with_client(TRUSTED_PEER, [(b"x-forwarded-for", chain.encode("ascii"))])
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [
            (b"x-forwarded-for", b"203.0.113.77"),
            (b"cf-connecting-ip", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == "203.0.113.77"
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [
            (b"x-forwarded-for", f"{REAL_CLIENT}, {CF_EDGE}".encode("ascii")),
            (b"cf-connecting-ip", b"203.0.113.60"),
        ],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == "203.0.113.60"
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_precedence_when_no_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"forwarded", b'for=203.0.113.44;proto=https')],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == "203.0.113.44"
    assert resolution.path == SourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
def test_xff_precedence_over_forwarded_and_cf_without_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [
            (b"x-forwarded-for", REAL_CLIENT.encode("ascii")),
            (b"forwarded", b'for="203.0.113.44"'),
            (b"cf-connecting-ip", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == REAL_CLIENT
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_invalid_xff_chain_fails_closed_to_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"x-forwarded-for", b"not-an-ip, also-bad")],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == TRUSTED_PEER
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDED


@pytest.mark.unit
def test_overlong_xff_chain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    long_chain = ",".join(["203.0.113.1"] * 40)
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"x-forwarded-for", long_chain.encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(
        request,
        peer_networks=settings.admin_trusted_proxy_networks,
    )
    assert resolution.source == TRUSTED_PEER
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDED


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_with_trusted_proxies(monkeypatch)
    request = _request_with_client(
        TRUSTED_PEER,
        [(b"x-forwarded-for", f"{REAL_CLIENT}, {CF_EDGE}".encode("ascii"))],
    )
    caplog.set_level(logging.INFO, logger="app.proxy_trust")
    caplog.set_level(logging.INFO, logger="app.admin_auth")
    admin_auth.resolve_client_source(request, settings)
    combined = caplog.text + str(caplog.records)
    assert REAL_CLIENT not in combined
    assert CF_EDGE not in combined
    assert "x-forwarded-for" not in combined.lower()
    assert any(
        record.__dict__.get("source_resolution_path") == "xff_trusted_chain"
        for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_source_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, _login, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        with patch("app.proxy_trust.immediate_peer_host", return_value=TRUSTED_PEER):
            assert _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CF_EDGE}"},
            ).status_code == 401
            assert _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.2, {REAL_CLIENT}, {CF_EDGE}"},
            ).status_code == 401
            blocked = _login(
                username="ghost",
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.3, {REAL_CLIENT}, {CF_EDGE}"},
            )
    assert blocked.status_code == 429
    assert len(store.rows) == 1


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips ''" in text
    assert "FORWARDED_ALLOW_IPS" in text
    assert "ADMIN_TRUSTED_PROXY_IPS" in text
    assert "10.0.0.0/8" in text


@pytest.mark.unit
def test_admin_auth_doc_matches_render_configuration() -> None:
    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    render = RENDER_YAML.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_IPS" in doc
    assert "--forwarded-allow-ips ''" in doc
    assert "admin_proxy_trust" in doc
    assert render.strip() and doc.strip()
    for snippet in ("10.0.0.0/8", "FORWARDED_ALLOW_IPS", "--forwarded-allow-ips ''"):
        assert snippet in render
        assert snippet in doc


@pytest.mark.unit
def test_health_reports_proxy_trust_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_proxy_trust"] == {
        "mode": "peer_allowlist",
        "uvicorn_forwarded_allow_ips": "",
    }


@pytest.mark.unit
def test_parse_trusted_proxy_networks_skips_invalid_entries() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8,not-valid,127.0.0.1")
    assert len(networks) == 2


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"server did not listen on {host}:{port}")


@contextmanager
def _uvicorn_server(
    *,
    port: int,
    env: dict[str, str],
) -> Generator[str, None, None]:
    merged = dict(env)
    merged.setdefault("DATABASE_URL", "")
    merged.setdefault("ADMIN_USERNAME", TEST_USERNAME)
    merged.setdefault("ADMIN_PASSWORD_HASH", TEST_HASH)
    merged.setdefault("ADMIN_SESSION_SECRET", TEST_SECRET)
    merged.setdefault("BASE_URL", f"http://127.0.0.1:{port}")
    merged.setdefault("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    merged.setdefault("FORWARDED_ALLOW_IPS", "")
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
        "",
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env={**os.environ, **merged},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_port("127.0.0.1", port)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_uvicorn_deployment_proxy_config_health() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with _uvicorn_server(port=port, env={}) as base:
        response = httpx.get(f"{base}/health", timeout=5.0)
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["admin_proxy_trust"]["mode"] == "peer_allowlist"
        assert payload["admin_proxy_trust"]["uvicorn_forwarded_allow_ips"] == ""


@pytest.mark.integration
def test_uvicorn_preserves_tcp_peer_for_untrusted_xff() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with _uvicorn_server(port=port, env={}) as base:
        response = httpx.get(
            f"{base}/health",
            headers={"X-Forwarded-For": "203.0.113.99"},
            timeout=5.0,
        )
        payload = response.json()
        assert payload["admin_proxy_trust"]["mode"] == "peer_allowlist"
        # Direct httpx peer is loopback; spoofed XFF must not change health payload.
        assert "203.0.113.99" not in json.dumps(payload)
