"""Tests for trusted-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourceResolution,
    SourceResolutionPath,
    normalize_client_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
)
from app.config import DEFAULT_TRUSTED_PROXY_CIDRS, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
RENDER_START_COMMAND = (
    "uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers "
    f"--forwarded-allow-ips='{RENDER_TRUSTED_IPS}'"
)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_CLOUDFLARE_PROXY_CIDRS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return get_settings()


def _request(
    *,
    peer: str = "198.51.100.10",
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


def _header(name: str, value: str) -> list[tuple[bytes, bytes]]:
    return [(name.lower().encode("ascii"), value.encode("ascii"))]


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
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("", None),
        ("not-an-ip", None),
        ("203.0.113.1" + "0" * 300, None),
    ],
)
def test_normalize_client_address(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    for header_value in ("203.0.113.99", "203.0.113.99, 198.51.100.20"):
        request = _request(
            peer="198.51.100.10",
            headers=_header("x-forwarded-for", header_value),
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution == ClientSourceResolution(
            "198.51.100.10",
            SourceResolutionPath.PEER_DIRECT,
        )


@pytest.mark.unit
def test_cloudflare_append_selects_connecting_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    request = _request(
        peer="10.0.0.1",
        headers=_header(
            "x-forwarded-for",
            "203.0.113.50, 198.51.100.77",
        ),
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "198.51.100.77",
        SourceResolutionPath.FORWARDED_RTL,
    )


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1,198.51.100.20",
    )
    request = _request(
        peer="10.0.0.1",
        headers=_header(
            "x-forwarded-for",
            "203.0.113.10, 198.51.100.20",
        ),
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.10",
        SourceResolutionPath.FORWARDED_RTL,
    )


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    request = _request(
        peer="203.0.113.5",
        headers=_header("x-forwarded-for", "203.0.113.10, 10.0.0.1"),
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.5",
        SourceResolutionPath.UNTRUSTED_PEER,
    )


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
        ADMIN_CLOUDFLARE_PROXY_CIDRS="198.51.100.0/24",
    )
    request = _request(
        peer="10.0.0.1",
        headers=[
            *_header("x-forwarded-for", "203.0.113.99"),
            *_header("cf-connecting-ip", "203.0.113.77"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.address == "203.0.113.99"
    assert resolution.path == SourceResolutionPath.FORWARDED_RTL


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1,198.51.100.20",
        ADMIN_CLOUDFLARE_PROXY_CIDRS="198.51.100.0/24",
    )
    request = _request(
        peer="10.0.0.1",
        headers=[
            *_header("x-forwarded-for", "198.51.100.20"),
            *_header("cf-connecting-ip", "203.0.113.44"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.44",
        SourceResolutionPath.CF_CONNECTING_VERIFIED,
    )


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_before_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1,198.51.100.20",
        ADMIN_CLOUDFLARE_PROXY_CIDRS="198.51.100.0/24",
    )
    request = _request(
        peer="10.0.0.1",
        headers=[
            *_header("x-forwarded-for", "203.0.113.10, 198.51.100.20"),
            *_header("forwarded", 'for="203.0.113.88"'),
            *_header("cf-connecting-ip", "203.0.113.44"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.10",
        SourceResolutionPath.FORWARDED_RTL,
    )


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    request = _request(
        peer="10.0.0.1",
        headers=_header("forwarded", 'for="203.0.113.60";proto=https'),
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "203.0.113.60",
        SourceResolutionPath.FORWARDED_RFC7239,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::9", "2001:db8::9"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        (" 203.0.113.2 , 10.0.0.1", "203.0.113.2"),
        (" , , ", "unknown"),
        ("not-valid, 10.0.0.1", "unknown"),
    ],
)
def test_address_format_cases(
    monkeypatch: pytest.MonkeyPatch,
    xff: str,
    expected: str,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    request = _request(peer="10.0.0.1", headers=_header("x-forwarded-for", xff))
    resolution = resolve_admin_login_client_source(request, settings)
    if expected == "unknown":
        assert resolution.path == SourceResolutionPath.MISSING_OR_MALFORMED
    else:
        assert resolution.path == SourceResolutionPath.FORWARDED_RTL
    assert resolution.address == expected


@pytest.mark.unit
def test_overlong_forwarding_chain_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    hops = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(peer="10.0.0.1", headers=_header("x-forwarded-for", hops))
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution == ClientSourceResolution(
        "unknown",
        SourceResolutionPath.MISSING_OR_MALFORMED,
    )


@pytest.mark.unit
def test_privacy_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_IPS="10.0.0.1",
    )
    request = _request(
        peer="10.0.0.1",
        headers=_header("x-forwarded-for", "not-valid"),
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)
    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113" not in message
        assert "x-forwarded-for" not in message.lower()
        assert "not-valid" not in message


@pytest.mark.unit
def test_deployment_proxy_trust_settings_are_consistent() -> None:
    render_text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert RENDER_START_COMMAND in render_text
    assert 'ADMIN_TRUST_PROXY_HEADERS' in render_text
    assert 'value: "true"' in render_text
    assert f'value: "{RENDER_TRUSTED_IPS}"' in render_text

    docs = (REPO_ROOT / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")
    assert RENDER_TRUSTED_IPS in docs
    assert "--forwarded-allow-ips" in docs
    assert "resolve_admin_login_client_source" in docs


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not listen on {host}:{port}")


@pytest.fixture
def uvicorn_proxy_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[str]:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$test")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "integration-secret-32chars-minimum-ok")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "127.0.0.1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    log_path = tmp_path / "uvicorn.log"
    process = subprocess.Popen(
        [
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
            "127.0.0.1",
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.integration
def test_uvicorn_proxy_chain_matches_application_resolver(
    uvicorn_proxy_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import admin_auth

    admin_auth.reset_login_rate_limiter()
    settings = get_settings()
    assert settings.admin_trust_proxy_headers is True

    with httpx.Client(base_url=uvicorn_proxy_server, timeout=5.0) as http:
        response = http.get(
            "/health",
            headers={"X-Forwarded-For": "203.0.113.50, 198.51.100.88"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    scope = {
        "type": "http",
        "headers": [
            (b"x-forwarded-for", b"203.0.113.50, 198.51.100.88"),
        ],
        "client": ("127.0.0.1", 12345),
        "method": "GET",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution == ClientSourceResolution(
        "198.51.100.88",
        SourceResolutionPath.FORWARDED_RTL,
    )
