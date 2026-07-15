"""Tests for trusted-proxy admin login client source resolution."""

from __future__ import annotations

import logging
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

from app import admin_auth
from app import db
from app.client_source import (
    SourceResolutionPath,
    normalize_ip_address,
    reset_untrusted_forwarding_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROXY = "10.0.0.1"
CLIENT_A = "203.0.113.77"
CLIENT_B = "198.51.100.10"


def _request_with_client(
    host: str,
    *,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (key.lower().encode("ascii"), value.encode("ascii"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _trusted_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_source_telemetry() -> None:
    reset_untrusted_forwarding_telemetry()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.50", "203.0.113.50"),
        ("  203.0.113.9  ", "203.0.113.9"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.999.999", None),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    for header in (
        CLIENT_A,
        f"{CLIENT_A}, {CLIENT_B}",
        f"evil, {CLIENT_A}, {RENDER_PROXY}",
    ):
        request = _request_with_client(
            CLIENT_B,
            headers={"X-Forwarded-For": header},
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == CLIENT_B
        assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"203.0.113.99, {CLIENT_A}, {RENDER_PROXY}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path is SourceResolutionPath.XFF_TRUSTED_HOP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path is SourceResolutionPath.XFF_TRUSTED_HOP


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT_A}, untrusted.example, {RENDER_PROXY}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PROXY
    assert resolution.path is SourceResolutionPath.CONSERVATIVE_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "CF-Connecting-IP": CLIENT_A,
            "X-Forwarded-For": f"{CLIENT_B}, {RENDER_PROXY}",
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path is SourceResolutionPath.XFF_TRUSTED_HOP


@pytest.mark.unit
def test_direct_render_origin_cf_header_alone_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={"CF-Connecting-IP": CLIENT_A},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PROXY
    assert resolution.path is SourceResolutionPath.CONSERVATIVE_PEER


@pytest.mark.unit
def test_header_precedence_forwarded_used_when_xff_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": "bad-hop, not-an-ip",
            "Forwarded": f'for={CLIENT_B};proto=https, for="{RENDER_PROXY}"',
            "CF-Connecting-IP": CLIENT_A,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_B
    assert resolution.path is SourceResolutionPath.FORWARDED_TRUSTED_HOP


@pytest.mark.unit
def test_malformed_and_overlong_forwarding_data_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    overlong = ",".join([f"10.0.0.{index}" for index in range(40)])
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": overlong},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_PROXY
    assert resolution.path is SourceResolutionPath.CONSERVATIVE_PEER


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": None,
            "method": "POST",
            "path": "/admin/login",
        }
    )
    resolution = resolve_admin_login_client_source(request, get_settings())
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.UNKNOWN_PEER


@pytest.mark.unit
def test_legacy_admin_trust_proxy_headers_applies_default_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == CLIENT_A


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _trusted_settings(monkeypatch)
    keys: set[str] = set()
    for index in range(5):
        request = _request_with_client(
            RENDER_PROXY,
            headers={
                "X-Forwarded-For": f"203.0.113.{index}, {CLIENT_A}, {RENDER_PROXY}",
            },
        )
        resolution = resolve_admin_login_client_source(request, settings)
        keys.add(admin_auth.build_source_rate_limit_key(resolution.source))
    assert len(keys) == 1
    assert keys.pop() == admin_auth.build_source_rate_limit_key(CLIENT_A)


@pytest.mark.unit
def test_telemetry_logs_resolution_path_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT_A}, {RENDER_PROXY}"},
    )
    admission = db.AdminLoginAdmission(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with caplog.at_level(logging.INFO):
        with patch("app.admin_auth.db.db_connection") as db_conn, patch(
            "app.admin_auth.db.try_admit_admin_login",
            return_value=admission,
        ), patch(
            "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
            return_value=0,
        ):
            db_conn.return_value.__enter__.return_value = object()
            db_conn.return_value.__exit__.return_value = None
            admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    assert any(
        record.__dict__.get("source_resolution_path")
        == SourceResolutionPath.XFF_TRUSTED_HOP.value
        for record in caplog.records
    )
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert CLIENT_A not in joined
    assert RENDER_PROXY not in joined
    assert "X-Forwarded-For" not in joined


@pytest.mark.unit
def test_untrusted_forwarding_attempts_emit_sampled_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _trusted_settings(monkeypatch)
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": "only-spoof"},
    )
    with caplog.at_level(logging.INFO):
        for _ in range(100):
            resolve_admin_login_client_source(request, settings)
    assert any(
        "sampled untrusted forwarding attempt" in record.getMessage()
        for record in caplog.records
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_uvicorn_proxy_chain_matches_deployment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "127.0.0.1")
    port = _free_port()
    command = [
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
    ]
    env = {key: value for key, value in __import__("os").environ.items()}
    env["ADMIN_TRUSTED_PROXY_IPS"] = "127.0.0.1"
    env.setdefault("DATABASE_URL", "")
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with httpx.Client() as client:
                    if client.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                        break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("uvicorn did not become ready")

        headers = {
            "X-Forwarded-For": f"203.0.113.99, {CLIENT_A}, 127.0.0.1",
        }
        with httpx.Client() as client:
            health = client.get(f"http://127.0.0.1:{port}/health", headers=headers)
            assert health.json()["admin_source_trust"] == "trusted_proxy_boundary"

            settings = get_settings()
            scope = {
                "type": "http",
                "headers": [
                    (b"x-forwarded-for", headers["X-Forwarded-For"].encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
                "method": "POST",
                "path": "/admin/login",
            }
            resolution = resolve_admin_login_client_source(Request(scope), settings)
            assert resolution.source == CLIENT_A
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
