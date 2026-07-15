"""Tests for verified-proxy admin login client source resolution (#239)."""

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
from app.config import get_settings
from app.main import app
from app.proxy_trust import (
    SourceResolutionPath,
    normalize_client_address,
    parse_forwarded_header,
    parse_x_forwarded_for,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_CIDRS = "10.0.0.0/8,172.16.0.0/12,100.64.0.0/10"
TRUSTED_RENDER_PEER = "10.0.0.55"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"
CLOUDFLARE_PROXY = "203.0.113.10"


def _asgi_with_client_host(client_host: str):
    async def middleware(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (client_host, 12345)
        await app(scope, receive, send)

    return middleware


def _request_with_client(host: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.1.1", None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_parse_x_forwarded_for_skips_invalid_and_empty_elements() -> None:
    header = " 203.0.113.1 , , invalid , 10.0.0.2 "
    assert parse_x_forwarded_for(header) == ["203.0.113.1", "10.0.0.2"]


@pytest.mark.unit
def test_parse_forwarded_header_extracts_for_values() -> None:
    header = 'for="203.0.113.5";proto=https, for=2001:db8::2'
    assert parse_forwarded_header(header) == ["203.0.113.5", "2001:db8::2"]


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS="",
        ADMIN_TRUST_PROXY_HEADERS="false",
    )
    for header in (
        b"203.0.113.99",
        b"203.0.113.99, 10.0.0.1",
    ):
        request = _request_with_client(
            "198.51.100.10",
            headers=[(b"x-forwarded-for", header)],
        )
        result = resolve_admin_login_client_source(request, settings)
        assert result.source == "198.51.100.10"
        assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode(),
            )
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", REAL_CLIENT.encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_uses_rightmost_untrusted_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_CLIENT}, 198.51.100.10, {REAL_CLIENT}".encode(),
            )
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_untrusted_peer_with_trusted_hop_claims_ignores_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        "198.51.100.10",
        headers=[(b"x-forwarded-for", f"{SPOOFED_CLIENT}, {TRUSTED_RENDER_PEER}".encode())],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.10"
    assert result.path == SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cloudflare_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
        ADMIN_TRUST_CLOUDFLARE_CONNECTING_IP="true",
        ADMIN_CLOUDFLARE_PROXY_CIDRS=f"{CLOUDFLARE_PROXY}/32",
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode()),
            (b"x-forwarded-for", b""),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == TRUSTED_RENDER_PEER
    assert result.path == SourceResolutionPath.TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_before_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
        ADMIN_TRUST_CLOUDFLARE_CONNECTING_IP="true",
        ADMIN_CLOUDFLARE_PROXY_CIDRS=f"{CLOUDFLARE_PROXY}/32",
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", REAL_CLIENT.encode()),
            (b"forwarded", b'for="198.51.100.10"'),
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode()),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == SourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_requires_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=f"{RENDER_TRUSTED_CIDRS},{CLOUDFLARE_PROXY}/32",
        ADMIN_TRUST_CLOUDFLARE_CONNECTING_IP="true",
        ADMIN_CLOUDFLARE_PROXY_CIDRS=f"{CLOUDFLARE_PROXY}/32",
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", CLOUDFLARE_PROXY.encode()),
            (b"cf-connecting-ip", REAL_CLIENT.encode()),
        ],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_overlong_forwarding_header_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", b"x" * 5000)],
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == TRUSTED_RENDER_PEER
    assert result.path == SourceResolutionPath.TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUSTED_PROXY_CIDRS=RENDER_TRUSTED_CIDRS,
    )
    request = _request_with_client(
        TRUSTED_RENDER_PEER,
        headers=[(b"x-forwarded-for", REAL_CLIENT.encode())],
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    for record in caplog.records:
        message = record.getMessage()
        assert REAL_CLIENT not in message
        assert TRUSTED_RENDER_PEER not in message
        if hasattr(record, "source_resolution_path"):
            assert REAL_CLIENT not in str(record.source_resolution_path)


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_auth._password_hasher.hash("secret"))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)

    from tests.test_admin_auth import (  # noqa: PLC0415
        FakeRateLimitStore,
        _parse_login_form,
        mock_db_connection,
        shared_rate_limiter,
    )

    trusted_client = TestClient(
        _asgi_with_client_host(TRUSTED_RENDER_PEER),
        follow_redirects=False,
    )
    store = FakeRateLimitStore()

    def _post_login(headers: dict[str, str]) -> Any:
        with mock_db_connection():
            csrf_token, cookies = _parse_login_form(trusted_client.get("/admin/login"))
            return trusted_client.post(
                "/admin/login",
                data={
                    "username": "ghost",
                    "password": "wrong",
                    "csrf_token": csrf_token,
                },
                cookies=cookies,
                headers=headers,
            )

    with shared_rate_limiter(store):
        for index in range(4):
            spoofed = f"198.51.100.{index}"
            response = _post_login(
                {"X-Forwarded-For": f"{spoofed}, {REAL_CLIENT}"},
            )
            if index < 2:
                assert response.status_code == 401
            else:
                assert response.status_code == 429

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert len(store.rows) == 1
    assert source_key in store.rows


@pytest.mark.unit
def test_verify_proxy_trust_config_script_passes() -> None:
    from scripts.verify_proxy_trust_config import verify_proxy_trust_config

    assert verify_proxy_trust_config() == []
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    admin_auth_doc = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")

    assert "--no-proxy-headers" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "10.0.0.0/8" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in admin_auth_doc
    assert "right-most untrusted hop" in admin_auth_doc
    assert "left-most" not in admin_auth_doc.lower() or "never" in admin_auth_doc.lower()


@pytest.mark.unit
@pytest.mark.integration
def test_uvicorn_no_proxy_headers_preserves_verified_source_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        admin_auth._password_hasher.hash("secret"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_TRUSTED_CIDRS)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-proxy-headers",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                "uvicorn did not become ready "
                f"stdout={stdout!r} stderr={stderr!r}"
            )

        settings = get_settings()
        scope = {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode()),
            ],
            "client": (TRUSTED_RENDER_PEER, 12345),
            "method": "GET",
            "path": "/health",
        }
        request = Request(scope)
        result = resolve_admin_login_client_source(request, settings)
        assert result.source == REAL_CLIENT
        assert result.path == SourceResolutionPath.XFF_TRUSTED_CHAIN
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
