"""Unit and integration tests for trusted admin login client source resolution."""

from __future__ import annotations

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
    DEFAULT_RENDER_FORWARDED_ALLOW_IPS,
    DEFAULT_RENDER_TRUSTED_PROXY_IPS,
    SourceResolutionPath,
    client_ip,
    reset_source_resolution_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_TRUSTED_PEER = "10.0.0.5"
RENDER_TRUSTED_PROXY_SPEC = f"{RENDER_TRUSTED_PEER},127.0.0.1"
CLOUDFLARE_EDGE = "104.16.0.1"
REAL_CLIENT = "203.0.113.77"
SPOOFED_CLIENT = "198.51.100.99"


def _request_with_client(
    host: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_proxy_ips: str = "",
    forward_proxy_ips: str = "",
    legacy_trust: bool = False,
) -> Any:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_FORWARD_PROXY_IPS", raising=False)
    if trusted_proxy_ips:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted_proxy_ips)
    if forward_proxy_ips:
        monkeypatch.setenv("ADMIN_TRUSTED_FORWARD_PROXY_IPS", forward_proxy_ips)
    if legacy_trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    for header_value in (
        SPOOFED_CLIENT,
        f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
    ):
        request = _request_with_client(
            REAL_CLIENT,
            headers=[(b"x-forwarded-for", header_value.encode("ascii"))],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == REAL_CLIENT
        assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC,
        forward_proxy_ips=CLOUDFLARE_EDGE,
    )
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{SPOOFED_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}".encode("ascii"),
            ),
            (b"cf-connecting-ip", REAL_CLIENT.encode("ascii")),
            (b"cf-ray", b"abc123"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC,
        forward_proxy_ips=CLOUDFLARE_EDGE,
    )
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}, {RENDER_TRUSTED_PEER}".encode("ascii"),
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    request = _request_with_client(
        "203.0.113.50",
        headers=[
            (
                b"x-forwarded-for",
                f"{REAL_CLIENT}, {RENDER_TRUSTED_PEER}".encode("ascii"),
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_cloudflare_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    direct_request = _request_with_client(
        REAL_CLIENT,
        headers=[
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode("ascii")),
            (b"cf-ray", b"fake-ray"),
        ],
    )
    direct_resolution = resolve_admin_login_client_source(direct_request, settings)
    assert direct_resolution.source == REAL_CLIENT
    assert direct_resolution.path is SourceResolutionPath.DIRECT_PEER

    trusted_peer_request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (b"cf-connecting-ip", SPOOFED_CLIENT.encode("ascii")),
            (b"cf-ray", b"fake-ray"),
        ],
    )
    trusted_resolution = resolve_admin_login_client_source(trusted_peer_request, settings)
    assert trusted_resolution.source == "unknown"
    assert trusted_resolution.path is SourceResolutionPath.TRUSTED_PEER_UNKNOWN


@pytest.mark.unit
def test_header_precedence_cf_connecting_ip_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC,
        forward_proxy_ips=CLOUDFLARE_EDGE,
    )
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (b"x-forwarded-for", f"{SPOOFED_CLIENT}, {REAL_CLIENT}".encode("ascii")),
            (b"forwarded", f'for="{SPOOFED_CLIENT}";proto=https'.encode("ascii")),
            (b"cf-connecting-ip", REAL_CLIENT.encode("ascii")),
            (b"cf-ray", b"edge-ray"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.1  ", "203.0.113.1"),
    ],
)
def test_address_formats_normalize_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    settings = _settings(monkeypatch)
    request = _request_with_client(raw)
    assert client_ip(request, settings) == expected


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data_resolve_unknown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    overlong = ", ".join([f"10.0.0.{index}" for index in range(40)])
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", overlong.encode("ascii"))],
    )
    with caplog.at_level(logging.INFO):
        resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.TRUSTED_PEER_UNKNOWN

    request_invalid = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"not-an-ip")],
    )
    resolution_invalid = resolve_admin_login_client_source(request_invalid, settings)
    assert resolution_invalid.source == "unknown"


@pytest.mark.unit
def test_single_hop_xff_on_trusted_peer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    keys: set[str] = set()
    for index in range(5):
        request = _request_with_client(
            RENDER_TRUSTED_PEER,
            headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode("ascii"))],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "unknown"
        keys.add(admin_auth.build_source_rate_limit_key(resolution.source))
    assert len(keys) == 1


@pytest.mark.unit
def test_rotating_spoofed_headers_do_not_create_new_limiter_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    keys: set[str] = set()
    for index in range(5):
        request = _request_with_client(
            REAL_CLIENT,
            headers=[(b"x-forwarded-for", f"203.0.113.{index}".encode("ascii"))],
        )
        source = client_ip(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert keys == {admin_auth.build_source_rate_limit_key(REAL_CLIENT)}


@pytest.mark.unit
def test_privacy_telemetry_and_logs_exclude_raw_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch, trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC)
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[(b"x-forwarded-for", b"not-an-ip")],
    )
    with caplog.at_level(logging.INFO):
        resolve_admin_login_client_source(request, settings)

    combined = caplog.text
    assert "x-forwarded-for" not in combined.lower()
    assert SPOOFED_CLIENT not in combined
    assert any(
        getattr(record, "source_resolution_path", None)
        for record in caplog.records
    )


@pytest.mark.unit
def test_forwarded_header_chain_resolves_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        trusted_proxy_ips=RENDER_TRUSTED_PROXY_SPEC,
        forward_proxy_ips=CLOUDFLARE_EDGE,
    )
    request = _request_with_client(
        RENDER_TRUSTED_PEER,
        headers=[
            (
                b"forwarded",
                (
                    f'for="{REAL_CLIENT}";proto=https, for="{CLOUDFLARE_EDGE}";proto=https'
                ).encode("ascii"),
            ),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.FORWARDED


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "forwarded-allow-ips" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "ADMIN_TRUSTED_FORWARD_PROXY_IPS" in render_yaml
    for cidr in DEFAULT_RENDER_TRUSTED_PROXY_IPS:
        assert cidr in render_yaml


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_uses_render_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, legacy_trust=True)
    request = _request_with_client(
        "10.0.0.9",
        headers=[(b"x-forwarded-for", f"{REAL_CLIENT}, 10.0.0.9".encode("ascii"))],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.X_FORWARDED_FOR


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_matches_trusted_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$test")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_PROXY_SPEC)
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARD_PROXY_IPS", CLOUDFLARE_EDGE)
    port = _free_port()
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["ADMIN_USERNAME"] = "operator"
    env["ADMIN_PASSWORD_HASH"] = "$argon2id$v=19$m=65536,t=3,p=4$test"
    env["ADMIN_SESSION_SECRET"] = "test-session-secret-32chars-minimum"
    env["ADMIN_TRUSTED_PROXY_IPS"] = RENDER_TRUSTED_PROXY_SPEC
    env["ADMIN_TRUSTED_FORWARD_PROXY_IPS"] = CLOUDFLARE_EDGE
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
        DEFAULT_RENDER_FORWARDED_ALLOW_IPS,
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("uvicorn did not become ready")

        health = httpx.get(
            f"http://127.0.0.1:{port}/health",
            headers={
                "X-Forwarded-For": f"{SPOOFED_CLIENT}, {REAL_CLIENT}",
                "CF-Connecting-IP": SPOOFED_CLIENT,
                "CF-Ray": "integration-ray",
            },
            timeout=2.0,
        )
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
    finally:
        process.terminate()
        process.wait(timeout=5)
