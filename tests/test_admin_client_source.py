"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    SOURCE_UNKNOWN,
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_client_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
    trusted_proxy_networks,
)
from app.config import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RENDER_YAML = _REPO_ROOT / "render.yaml"
_ADMIN_AUTH_DOC = _REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": None if peer is None else (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "x")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_ignored_without_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="false")
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99, 198.51.100.10")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution("198.51.100.10", SourceResolutionPath.DIRECT_PEER)


@pytest.mark.unit
def test_direct_spoof_ignored_with_untrusted_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER
    assert resolution.source == "198.51.100.10"


@pytest.mark.unit
def test_multi_value_xff_spoof_ignored_for_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.1, 203.0.113.2, 203.0.113.3")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[
            (
                b"x-forwarded-for",
                b"203.0.113.9, 203.0.113.50, 172.64.1.1",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.50",
        SourceResolutionPath.XFF_TRUSTED_CHAIN,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[(b"x-forwarded-for", b"203.0.113.77, 172.64.2.2")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.77"
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed_behind_untrusted_intermediary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="203.0.113.200",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.5, 172.64.1.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.200"
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="203.0.113.8",
        headers=[(b"cf-connecting-ip", b"203.0.113.99")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.8"
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_header_precedence_prefers_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[
            (b"x-forwarded-for", b"203.0.113.10, 172.64.1.1"),
            (b"forwarded", b'for="203.0.113.20";proto=https'),
            (b"cf-connecting-ip", b"203.0.113.30"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[
            (b"forwarded", b'for="203.0.113.44", for=172.64.3.3;proto=https'),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path == SourceResolutionPath.FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_only_after_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[(b"cf-connecting-ip", b"203.0.113.55")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.55"
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP_TRUSTED_EDGE


@pytest.mark.unit
def test_malformed_and_empty_xff_elements_fail_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[(b"x-forwarded-for", b" , not-an-ip, 172.64.1.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path == SourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_overlong_chain_fails_closed_to_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    hops = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(peer="10.0.0.5", headers=[(b"x-forwarded-for", hops.encode())])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "10.0.0.5"
    assert resolution.path == SourceResolutionPath.OVERLONG_CHAIN


@pytest.mark.unit
def test_missing_peer_uses_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(peer=None)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(SOURCE_UNKNOWN, SourceResolutionPath.MISSING_PEER)


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    request = _request(
        peer="10.0.0.5",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 172.64.1.1")],
    )
    caplog.set_level(logging.INFO)
    resolve_admin_login_client_source(request, settings)
    messages = [record.message for record in caplog.records if record.name.endswith("admin_client_source")]
    assert messages
    assert "203.0.113.50" not in caplog.text
    assert "172.64.1.1" not in caplog.text
    assert any(
        getattr(record, "resolution_path", None) == SourceResolutionPath.XFF_TRUSTED_CHAIN.value
        for record in caplog.records
    )


@pytest.mark.unit
def test_limiter_key_stable_for_rotating_spoofed_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    peer = "198.51.100.10"
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request(
                    peer=peer,
                    headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode())],
                ),
                settings,
            ).source
        )
        for index in range(10)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_limiter_and_audit_state_contain_no_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limiter rows store only digested keys — never raw addresses or header chains."""
    from argon2 import PasswordHasher

    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter, _login

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")

    store = FakeRateLimitStore()
    with shared_rate_limiter(store):
        for index in range(3):
            _login(
                password="wrong",
                headers={"X-Forwarded-For": f"203.0.113.{index}, 172.64.1.1"},
            )
    for limiter_key, row in store.rows.items():
        assert "203.0.113" not in limiter_key
        assert "x-forwarded-for" not in str(row).lower()
        assert len(limiter_key) == 64


@pytest.mark.unit
def test_deployment_proxy_settings_are_consistent() -> None:
    render_text = _RENDER_YAML.read_text(encoding="utf-8")
    doc_text = _ADMIN_AUTH_DOC.read_text(encoding="utf-8")

    assert "--no-proxy-headers" in render_text
    assert "--forwarded-allow-ips" in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert "ADMIN_TRUST_CLOUDFLARE_PROXIES" in render_text
    assert "--no-proxy-headers" in doc_text
    assert "right-to-left" in doc_text.lower() or "right-to-left" in doc_text


@pytest.mark.unit
def test_trusted_proxy_networks_include_platform_and_cloudflare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="true")
    networks = trusted_proxy_networks(settings)
    assert ipaddress_in_network("10.0.0.5", networks)
    assert ipaddress_in_network("172.64.1.1", networks)


def ipaddress_in_network(address: str, networks: tuple[object, ...]) -> bool:
    import ipaddress

    parsed = ipaddress.ip_address(address)
    return any(parsed in network for network in networks)


@pytest.mark.integration
def test_uvicorn_no_proxy_headers_preserves_peer_for_admin_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise deployment-like Uvicorn flags; spoofed XFF must not change limiter peer."""
    port = _free_port()
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    env = {
        **dict(__import__("os").environ),
        "ADMIN_TRUST_PROXY_HEADERS": "true",
        "BASE_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.fixtures.proxy_trust_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1",
        ],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health", timeout=20.0)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            response = client.get(
                "/source",
                headers={
                    "X-Forwarded-For": "203.0.113.9, 203.0.113.50, 172.64.1.1",
                },
            )
        payload = response.json()
        assert response.status_code == 200
        assert payload["source"] == "203.0.113.50"
        assert payload["path"] == "xff_trusted_chain"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, *, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - poll until timeout
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"service did not become healthy: {last_error}")
