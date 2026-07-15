"""Tests for verified proxy-hop admin login client source resolution (#239)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.client_source import (
    SourceResolutionPath,
    normalize_client_address,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

TEST_CF_EDGE = "103.21.244.8"
TEST_RENDER_PEER = "10.0.0.55"
TEST_CLIENT = "203.0.113.77"
TEST_OTHER_CLIENT = "203.0.113.88"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, *, trust_proxy: bool):
    if trust_proxy:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.mark.unit
def test_normalize_client_address_formats() -> None:
    assert normalize_client_address("203.0.113.1") == "203.0.113.1"
    assert normalize_client_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_client_address("2001:db8::1") == "2001:db8::1"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("::ffff:203.0.113.50") == "203.0.113.50"
    assert normalize_client_address("  203.0.113.2  ") == "203.0.113.2"
    assert normalize_client_address("") is None
    assert normalize_client_address("not-an-ip") is None
    assert normalize_client_address("203.0.113") is None


@pytest.mark.unit
def test_direct_spoof_ignored_without_trusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    for header_value in (
        TEST_CLIENT,
        f"{TEST_OTHER_CLIENT}, {TEST_RENDER_PEER}",
    ):
        request = _request_with_client(
            "198.51.100.10",
            headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
        )
        resolved = resolve_admin_login_client_source(request, settings)
        assert resolved.address == "198.51.100.10"
        assert resolved.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_spoof_ignored_when_proxy_trust_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=False)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"x-forwarded-for", f"{TEST_CLIENT}, {TEST_RENDER_PEER}".encode("ascii"))],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_RENDER_PEER
    assert resolved.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"203.0.113.99, {TEST_CLIENT}, {TEST_CF_EDGE}".encode("ascii"),
            ),
            (b"cf-connecting-ip", TEST_CLIENT.encode("ascii")),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_CLIENT
    assert resolved.path is SourceResolutionPath.CLOUDFLARE_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{TEST_CLIENT}, {TEST_CF_EDGE}".encode("ascii"),
            ),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_CLIENT
    assert resolved.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_partial_trust_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        "198.51.100.10",
        headers=[
            (
                b"x-forwarded-for",
                f"{TEST_CLIENT}, {TEST_CF_EDGE}".encode("ascii"),
            ),
            (b"cf-connecting-ip", TEST_CLIENT.encode("ascii")),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == "198.51.100.10"
    assert resolved.path is SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (b"cf-connecting-ip", TEST_CLIENT.encode("ascii")),
            (b"cf-ray", b"abc123"),
            (b"x-forwarded-for", f"{TEST_OTHER_CLIENT}, {TEST_RENDER_PEER}".encode("ascii")),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_OTHER_CLIENT
    assert resolved.path is SourceResolutionPath.FORWARDED_CHAIN


@pytest.mark.unit
def test_multiple_header_families_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (b"x-forwarded-for", f"{TEST_OTHER_CLIENT}, {TEST_CF_EDGE}".encode("ascii")),
            (b"cf-connecting-ip", TEST_CLIENT.encode("ascii")),
            (b"forwarded", f'for="{TEST_OTHER_CLIENT}";proto=https'.encode("ascii")),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_CLIENT
    assert resolved.path is SourceResolutionPath.CLOUDFLARE_CONNECTING_IP


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    overlong = ",".join(["203.0.113.1"] * 20)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"x-forwarded-for", overlong.encode("ascii"))],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == "unknown"
    assert resolved.path is SourceResolutionPath.MALFORMED_FORWARDING

    request_invalid = _request_with_client(
        TEST_RENDER_PEER,
        headers=[(b"x-forwarded-for", b"not-an-ip")],
    )
    resolved_invalid = resolve_admin_login_client_source(request_invalid, settings)
    assert resolved_invalid.address == "unknown"
    assert resolved_invalid.path is SourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = _request_with_client(
        TEST_RENDER_PEER,
        headers=[
            (
                b"forwarded",
                f'for="{TEST_CLIENT}";for="{TEST_CF_EDGE}";proto=https'.encode("ascii"),
            ),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_CLIENT
    assert resolved.path is SourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
def test_missing_peer_resolves_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": None,
            "method": "POST",
            "path": "/admin/login",
        }
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == "unknown"
    assert resolved.path is SourceResolutionPath.MISSING_SOURCE


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_source_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=False)
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_admin_login_client_source(
                _request_with_client(
                    "testclient",
                    headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode("ascii"))],
                ),
                settings,
            ).address
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_limiter_keys_do_not_store_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    resolved = resolve_admin_login_client_source(
        _request_with_client(
            TEST_RENDER_PEER,
            headers=[
                (
                    b"x-forwarded-for",
                    f"203.0.113.99, {TEST_CLIENT}, {TEST_CF_EDGE}".encode("ascii"),
                ),
            ],
        ),
        settings,
    )
    source_key = admin_auth.build_source_rate_limit_key(resolved.address)
    assert TEST_CLIENT not in source_key
    assert "203.0.113.99" not in source_key
    assert "x-forwarded-for" not in source_key
    assert len(source_key) == 64


@pytest.mark.unit
def test_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, trust_proxy=True)
    with caplog.at_level("INFO"):
        resolve_admin_login_client_source(
            _request_with_client(
                "198.51.100.10",
                headers=[(b"x-forwarded-for", b"203.0.113.50")],
            ),
            settings,
        )
    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.50" not in message
        assert "x-forwarded-for" not in message.lower()


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in content
    assert "ADMIN_TRUST_PROXY_HEADERS" in content
    assert "ADMIN_FORWARDED_ALLOW_IPS" in content
    assert 'value: "true"' in content


@pytest.mark.unit
def test_admin_auth_doc_matches_runtime_trust_model() -> None:
    content = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    assert "ADMIN_TRUSTED_PROXY_NETWORKS" in content
    assert "ADMIN_FORWARDED_ALLOW_IPS" in content
    assert "CF-Connecting-IP" in content
    assert "right-to-left" in content
    assert "ADMIN_TRUST_PROXY_HEADERS=false" in content


@pytest.mark.unit
def test_health_reports_proxy_trust_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_FORWARDED_ALLOW_IPS", "127.0.0.1")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["admin_client_source_trust"] == "proxy_verified"
    assert payload["admin_forwarded_allow_ips"] == "127.0.0.1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def uvicorn_proxy_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run uvicorn with the same narrow forwarded-allow-ips used in render.yaml."""
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "",
            "ADMIN_USERNAME": "",
            "ADMIN_PASSWORD_HASH": "",
            "ADMIN_SESSION_SECRET": "",
            "BASE_URL": f"http://127.0.0.1:{port}",
            "ADMIN_TRUST_PROXY_HEADERS": "true",
            "ADMIN_FORWARDED_ALLOW_IPS": "127.0.0.1",
        }
    )
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
        "127.0.0.1",
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
    origin = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            response = httpx.get(f"{origin}/health", timeout=1.0)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        process.kill()
        raise RuntimeError("uvicorn did not become ready")
    try:
        yield origin
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.integration
def test_uvicorn_integration_trusts_verified_forwarding_chain(
    uvicorn_proxy_server: str,
) -> None:
    response = httpx.get(
        f"{uvicorn_proxy_server}/health",
        headers={
            "X-Forwarded-For": f"203.0.113.99, {TEST_CLIENT}, {TEST_CF_EDGE}",
            "CF-Connecting-IP": TEST_CLIENT,
        },
        timeout=5.0,
    )
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["admin_client_source_trust"] == "proxy_verified"
    assert payload["admin_forwarded_allow_ips"] == "127.0.0.1"


@pytest.mark.integration
def test_uvicorn_integration_ignores_spoofed_vendor_headers_without_edge_hop(
    uvicorn_proxy_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        "127.0.0.1",
        headers=[
            (b"cf-connecting-ip", TEST_CLIENT.encode("ascii")),
            (b"x-forwarded-for", f"{TEST_OTHER_CLIENT}, 127.0.0.1".encode("ascii")),
        ],
    )
    resolved = resolve_admin_login_client_source(request, settings)
    assert resolved.address == TEST_OTHER_CLIENT
    assert resolved.path is SourceResolutionPath.FORWARDED_CHAIN

    health = httpx.get(
        f"{uvicorn_proxy_server}/health",
        headers={
            "CF-Connecting-IP": TEST_CLIENT,
            "X-Forwarded-For": f"{TEST_OTHER_CLIENT}, 127.0.0.1",
        },
        timeout=5.0,
    )
    assert health.json()["admin_client_source_trust"] == "proxy_verified"
