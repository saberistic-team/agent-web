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

from app import admin_auth
from app.admin_proxy_trust import (
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_client_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = (
    "argon2id$v=19$m=65536,t=3,p=4$"
    "dGVzdHNhbHQ$testhash"
)
TEST_SECRET = "test-session-secret-32chars-minimum"


def _request(
    *,
    peer_host: str = "198.51.100.10",
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (peer_host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    peer_host: str = "198.51.100.10",
    headers: dict[str, str] | None = None,
    trust_proxy: bool = False,
    trusted_cidrs: str = "10.0.0.0/8,127.0.0.1",
    cloudflare_cidrs: str = "104.16.0.0/13",
) -> ClientSourceResolution:
    if trust_proxy:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", trusted_cidrs)
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUST_CIDRS", cloudflare_cidrs)
    settings = get_settings()
    return resolve_admin_login_client_source(
        _request(peer_host=peer_host, headers=headers),
        settings,
    )


@pytest.fixture(autouse=True)
def _proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_CLOUDFLARE_TRUST_CIDRS", raising=False)
    reset_source_resolution_telemetry()
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("999.999.999.999") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for header in (
        "203.0.113.99",
        "203.0.113.99, 203.0.113.100",
    ):
        result = _resolve(
            monkeypatch,
            peer_host="198.51.100.10",
            headers={"X-Forwarded-For": header},
            trust_proxy=False,
        )
        assert result.address == "198.51.100.10"
        assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={
            "X-Forwarded-For": "203.0.113.99, 198.51.100.10, 104.16.0.1",
            "CF-Connecting-IP": "198.51.100.10",
        },
    )
    assert result.address == "198.51.100.10"
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client_without_cf_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={"X-Forwarded-For": "203.0.113.77, 10.0.0.5"},
    )
    assert result.address == "203.0.113.77"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="198.51.100.20",
        trust_proxy=True,
        headers={"X-Forwarded-For": "203.0.113.55, 10.0.0.5"},
    )
    assert result.address == "198.51.100.20"
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={
            "CF-Connecting-IP": "203.0.113.99",
            "X-Forwarded-For": "203.0.113.99, 198.51.100.10",
        },
    )
    assert result.address == "198.51.100.10"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_header_precedence_cf_over_conflicting_xff_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={
            "CF-Connecting-IP": "198.51.100.10",
            "X-Forwarded-For": "203.0.113.1, 104.16.0.1",
            "Forwarded": 'for="203.0.113.2"',
        },
    )
    assert result.address == "198.51.100.10"
    assert result.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_rfc_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={"Forwarded": 'for="203.0.113.44", for=10.0.0.5'},
    )
    assert result.address == "203.0.113.44"
    assert result.path is SourceResolutionPath.FORWARDED_RFC


@pytest.mark.unit
def test_overlong_forward_chain_fails_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={"X-Forwarded-For": chain},
    )
    assert result.address == "10.0.0.5"
    assert result.path is SourceResolutionPath.PEER_FALLBACK


@pytest.mark.unit
def test_malformed_addresses_and_empty_elements_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _resolve(
        monkeypatch,
        peer_host="10.0.0.5",
        trust_proxy=True,
        headers={"X-Forwarded-For": " , not-an-ip, 203.0.113.88, 10.0.0.5"},
    )
    assert result.address == "203.0.113.88"
    assert result.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_missing_peer_resolves_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": None,
            "method": "POST",
            "path": "/admin/login",
        }
    )
    settings = get_settings()
    result = resolve_admin_login_client_source(request, settings)
    assert result.address == "unknown"
    assert result.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    keys: set[str] = set()
    for index in range(5):
        resolution = resolve_admin_login_client_source(
            _request(
                peer_host="198.51.100.10",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            ),
            settings,
        )
        keys.add(admin_auth.build_source_rate_limit_key(resolution.address))
    assert len(keys) == 1
    assert resolution.address == "198.51.100.10"


@pytest.mark.unit
def test_trusted_peer_rotating_leftmost_spoof_shares_one_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    settings = get_settings()
    keys: set[str] = set()
    for index in range(5):
        resolution = resolve_admin_login_client_source(
            _request(
                peer_host="10.0.0.5",
                headers={
                    "X-Forwarded-For": (
                        f"203.0.113.{index}, 198.51.100.10, 10.0.0.5"
                    )
                },
            ),
            settings,
        )
        keys.add(admin_auth.build_source_rate_limit_key(resolution.address))
    assert len(keys) == 1
    assert resolution.address == "198.51.100.10"


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    _resolve(
        monkeypatch,
        peer_host="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
        trust_proxy=False,
    )
    combined = caplog.text.lower()
    assert "203.0.113.99" not in combined
    assert "198.51.100.10" not in combined
    assert "x-forwarded-for" not in combined
    assert any(
        getattr(record, "resolution_path", None) is not None
        or getattr(record, "invalid_forwarding", None) is not None
        for record in caplog.records
    )


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_present_and_consistent() -> None:
    render_text = RENDER_YAML.read_text(encoding="utf-8")
    assert "--proxy-headers" in render_text
    assert "--forwarded-allow-ips" in render_text
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_text
    assert 'value: "true"' in render_text or "value: 'true'" in render_text
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_text
    assert "ADMIN_CLOUDFLARE_TRUST_CIDRS" in render_text
    assert "10.0.0.0/8" in render_text
    assert "127.0.0.1" in render_text
    assert "104.16.0.0/13" in render_text

    doc = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in doc
    assert "ADMIN_CLOUDFLARE_TRUST_CIDRS" in doc
    assert "--forwarded-allow-ips" in doc
    assert "right-to-left" in doc
    for token in ("10.0.0.0/8", "127.0.0.1"):
        assert token in doc
        assert token in render_text


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


@pytest.mark.integration
def test_uvicorn_deployment_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same uvicorn forwarded-header flags used on Render."""
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1,10.0.0.0/8")
    monkeypatch.setenv("ADMIN_CLOUDFLARE_TRUST_CIDRS", "104.16.0.0/13")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = {
        **dict(__import__("os").environ),
        "DATABASE_URL": "",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "BASE_URL": "http://testserver",
        "ADMIN_TRUST_PROXY_HEADERS": "true",
        "ADMIN_TRUSTED_PROXY_CIDRS": "127.0.0.1,10.0.0.0/8",
        "ADMIN_CLOUDFLARE_TRUST_CIDRS": "104.16.0.0/13",
    }
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
    process = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as http:
            health = http.get("/health")
            assert health.status_code == 200

            spoofed = http.get(
                "/admin/login",
                headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
            )
            assert spoofed.status_code in {200, 503}

            from app.config import get_settings

            scope: dict[str, Any] = {
                "type": "http",
                "headers": [
                    (b"x-forwarded-for", b"203.0.113.99, 198.51.100.10, 127.0.0.1"),
                    (b"cf-connecting-ip", b"198.51.100.10"),
                ],
                "client": ("127.0.0.1", port),
                "method": "GET",
                "path": "/admin/login",
            }
            settings = get_settings()
            resolution = resolve_admin_login_client_source(Request(scope), settings)
            assert resolution.address == "198.51.100.10"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
