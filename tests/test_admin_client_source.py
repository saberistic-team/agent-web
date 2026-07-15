"""Tests for verified-proxy admin login client-source resolution (#239)."""

from __future__ import annotations

import json
import logging
import os
import signal
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
from app.admin_client_source import (
    ClientSourceResolutionPath,
    reset_untrusted_header_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.trusted_proxy_boundary import (
    TrustedProxyBoundary,
    normalize_ip_address,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_LB = "10.0.0.1"
RENDER_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
CF_EDGE = "172.64.0.1"


def _request(
    *,
    peer: str | None = "198.51.100.10",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _configure_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDING_CIDRS", RENDER_CIDRS)
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_FORWARDING", "true")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_untrusted_header_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()

    for headers in (
        [(b"x-forwarded-for", b"203.0.113.99")],
        [(b"x-forwarded-for", b"203.0.113.99, 203.0.113.100")],
    ):
        request = _request(peer="198.51.100.10", headers=headers)
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "198.51.100.10"
        assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (
                b"x-forwarded-for",
                b"203.0.113.99, 198.51.100.10, 172.64.0.1",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "198.51.100.10"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_FOR


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.50"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_FOR


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer="203.0.113.200",
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.200"
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.77"),
            (b"x-forwarded-for", b"203.0.113.88"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.88"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_FOR


@pytest.mark.unit
def test_cf_connecting_ip_used_only_with_verified_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"cf-connecting-ip", b"203.0.113.60"),
            (b"x-forwarded-for", f"203.0.113.60, {CF_EDGE}, {RENDER_LB}".encode()),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.60"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_FOR


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[
            (b"x-forwarded-for", b"203.0.113.10, 10.0.0.1"),
            (b"forwarded", b'for=203.0.113.20;proto=https'),
            (b"cf-connecting-ip", b"203.0.113.30"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.10"


@pytest.mark.unit
def test_forwarded_header_used_when_xff_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"forwarded", b'for=203.0.113.44;proto=https, for=10.0.0.1')],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.44"
    assert resolution.path is ClientSourceResolutionPath.FORWARDED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.5", "203.0.113.5"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_address_normalization(raw: str, expected: str | None) -> None:
    assert normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_overlong_forwarded_chain_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(peer=RENDER_LB, headers=[(b"x-forwarded-for", chain.encode())])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_whitespace_and_empty_xff_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b" 203.0.113.7 , , 10.0.0.1 ")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.7"


@pytest.mark.unit
def test_untrusted_headers_emit_sampled_telemetry_without_raw_ips(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request(
        peer="198.51.100.10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
    )
    with patch(
        "app.admin_client_source._UNTRUSTED_HEADER_SAMPLE_RATE",
        1,
    ):
        resolve_admin_login_client_source(request, settings)

    assert any(
        record.message == "Admin login client source ignored forwarding headers"
        for record in caplog.records
    )
    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.99" not in message
        assert "198.51.100.10" not in message


@pytest.mark.unit
def test_boundary_ipv4_mapped_and_duplicate_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"::ffff:203.0.113.9, 10.0.0.1, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.9"


@pytest.mark.unit
def test_missing_peer_uses_unknown_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(peer=None)
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.UNKNOWN


@pytest.mark.unit
def test_client_ip_wrapper_matches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_trusted_proxies(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_LB,
        headers=[(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    )
    assert admin_auth.client_ip(request, settings) == "203.0.113.50"


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    start_script = (REPO_ROOT / "scripts/start_web.sh").read_text(encoding="utf-8")

    assert "ADMIN_TRUSTED_PROXY_CIDRS" in render_yaml
    assert "ADMIN_TRUSTED_FORWARDING_CIDRS" in render_yaml
    assert "ADMIN_TRUST_CLOUDFLARE_FORWARDING" in render_yaml
    assert "bash scripts/start_web.sh" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_CIDRS" in start_script
    assert "--forwarded-allow-ips" in start_script


@pytest.mark.unit
def test_health_reports_client_source_trust_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import health

    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", RENDER_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDING_CIDRS", RENDER_CIDRS)
    payload = health()
    trust = payload["admin_client_source_trust"]
    assert trust["immediate_peer_cidrs_configured"] is True
    assert trust["forwarding_cidrs_configured"] is True
    assert trust["cloudflare_forwarding_enabled"] is True
    assert "203.0.113" not in json.dumps(trust)


@pytest.mark.unit
def test_trusted_proxy_boundary_unit_cases() -> None:
    cidrs = tuple(part.strip() for part in RENDER_CIDRS.split(","))
    boundary = TrustedProxyBoundary(
        immediate_peer_cidrs=cidrs,
        forwarding_chain_cidrs=cidrs,
    )
    assert boundary.client_from_forwarded_for("203.0.113.1, 10.0.0.1") == "203.0.113.1"
    assert boundary.client_from_forwarded_for("invalid, 10.0.0.1") is None


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Uvicorn proxy middleware with the same start-script settings."""
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1")
    monkeypatch.setenv("ADMIN_TRUSTED_FORWARDING_CIDRS", "127.0.0.1")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_FORWARDING", "false")

    env = os.environ.copy()
    env["PORT"] = "8765"
    env["ADMIN_TRUSTED_PROXY_CIDRS"] = "127.0.0.1"
    env["ADMIN_TRUSTED_FORWARDING_CIDRS"] = "127.0.0.1"
    env["ADMIN_TRUST_CLOUDFLARE_FORWARDING"] = "false"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--forwarded-allow-ips",
            "127.0.0.1",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                httpx.get("http://127.0.0.1:8765/health", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("uvicorn did not become ready")

        trust = httpx.get("http://127.0.0.1:8765/health", timeout=5.0).json()
        assert trust["admin_client_source_trust"]["immediate_peer_cidrs_configured"]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
