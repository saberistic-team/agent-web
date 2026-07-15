"""Tests for verified-hop admin login client source resolution."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.client_source import (
    deployment_proxy_trust_summary,
    normalize_client_source,
    reset_spoof_telemetry_for_tests,
    resolve_client_source,
)
from app.config import get_settings, parse_trusted_proxy_networks

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PROXY = "10.0.0.1"
CLIENT_A = "203.0.113.77"
CLIENT_B = "203.0.113.88"
SPOOFED = "203.0.113.99"
ATTACKER = "198.51.100.10"
CF_EDGE = "104.16.0.1"


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    reset_spoof_telemetry_for_tests()


@pytest.fixture
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUST_CLOUDFLARE_EDGE", "true")


def _request(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


def _xff(*hops: str) -> str:
    return ", ".join(hops)


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    for xff in ("203.0.113.1", "203.0.113.1, 203.0.113.2"):
        request = _request(
            peer=ATTACKER,
            headers=[_header("x-forwarded-for", xff)],
        )
        resolution = resolve_client_source(request, settings)
        assert resolution.source == ATTACKER
        assert resolution.path == "immediate_peer"


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header("x-forwarded-for", _xff(SPOOFED, ATTACKER, CF_EDGE, RENDER_PROXY)),
        ],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == ATTACKER
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(proxy_trust_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[_header("x-forwarded-for", _xff(CLIENT_A, RENDER_PROXY))],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=ATTACKER,
        headers=[_header("x-forwarded-for", _xff(CLIENT_A, RENDER_PROXY))],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == ATTACKER
    assert resolution.path == "immediate_peer_untrusted"
    assert resolution.untrusted_header_attempt is True


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header("cf-connecting-ip", CLIENT_A),
            _header("x-forwarded-for", _xff(SPOOFED, RENDER_PROXY)),
        ],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == SPOOFED
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_cloudflare_hop_verified(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header("cf-connecting-ip", CLIENT_A),
            _header("x-forwarded-for", _xff(CF_EDGE, RENDER_PROXY)),
        ],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "cf_connecting_ip_verified"


@pytest.mark.unit
def test_header_precedence_xff_over_conflicting_forwarded_and_cf(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[
            _header("x-forwarded-for", _xff(CLIENT_A, RENDER_PROXY)),
            _header("cf-connecting-ip", CLIENT_B),
            _header("forwarded", f'for={CLIENT_B};proto=https'),
        ],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.source == CLIENT_A
    assert resolution.path == "xff_right_to_left"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.1 ", "203.0.113.1"),
        ("", "unknown"),
        ("not-an-ip", "unknown"),
    ],
)
def test_normalize_client_source_formats(raw: str, expected: str) -> None:
    assert normalize_client_source(raw) == expected


@pytest.mark.unit
def test_malformed_xff_empty_elements(proxy_trust_env: None) -> None:
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        headers=[_header("x-forwarded-for", "203.0.113.1,,10.0.0.1")],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "xff_malformed_or_overlong"
    assert resolution.source == RENDER_PROXY


@pytest.mark.unit
def test_overlong_xff_chain_rejected(proxy_trust_env: None) -> None:
    settings = get_settings()
    hops = ", ".join(f"203.0.113.{index}" for index in range(40))
    request = _request(
        peer=RENDER_PROXY,
        headers=[_header("x-forwarded-for", hops)],
    )
    resolution = resolve_client_source(request, settings)
    assert resolution.path == "xff_malformed_or_overlong"


@pytest.mark.unit
def test_rotating_spoofed_headers_share_one_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_client_source(
                _request(peer=ATTACKER, headers=[_header("x-forwarded-for", f"203.0.113.{i}")]),
                settings,
            ).source
        )
        for i in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_trusted_rotating_leftmost_spoofs_share_one_bucket(
    proxy_trust_env: None,
) -> None:
    settings = get_settings()
    keys = {
        admin_auth.build_source_rate_limit_key(
            resolve_client_source(
                _request(
                    peer=RENDER_PROXY,
                    headers=[
                        _header(
                            "x-forwarded-for",
                            _xff(f"203.0.113.{index}", ATTACKER, RENDER_PROXY),
                        )
                    ],
                ),
                settings,
            ).source
        )
        for index in range(5)
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_privacy_telemetry_and_logs_exclude_raw_addresses(
    proxy_trust_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    request = _request(
        peer=ATTACKER,
        headers=[_header("x-forwarded-for", SPOOFED)],
    )
    with caplog.at_level(logging.WARNING):
        resolution = resolve_client_source(request, settings)
    assert resolution.untrusted_header_attempt is True
    serialized = json.dumps(
        {
            "path": resolution.path,
            "header_family": resolution.header_family,
        }
    )
    assert SPOOFED not in serialized
    assert ATTACKER not in caplog.text


@pytest.mark.unit
def test_deployment_proxy_trust_summary_has_no_raw_ips(
    proxy_trust_env: None,
) -> None:
    summary = deployment_proxy_trust_summary(get_settings())
    blob = json.dumps(summary)
    assert "10.0.0.0" not in blob
    assert summary["resolution_strategy"] == "verified_hop_parse"


@pytest.mark.unit
def test_render_yaml_proxy_settings_are_consistent() -> None:
    render_yaml = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips 127.0.0.1" in render_yaml
    assert "ADMIN_TRUST_PROXY_HEADERS" in render_yaml
    assert "ADMIN_TRUSTED_PROXY_IPS" in render_yaml
    assert "ADMIN_TRUST_CLOUDFLARE_EDGE" in render_yaml
    assert 'value: "127.0.0.1"' in render_yaml


@pytest.mark.unit
def test_health_reports_admin_client_source(proxy_trust_env: None) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/health")
    payload = response.json()
    assert payload["admin_client_source"]["resolution_strategy"] == "verified_hop_parse"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def uvicorn_server(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    port = _free_port()
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.update(
        {
            "ADMIN_USERNAME": "operator",
            "ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$RdescudvJCsgt3q+bzlKd0c1vGz0W0ieC9hTJxGFl3A",
            "ADMIN_SESSION_SECRET": "test-session-secret-32chars-minimum",
            "BASE_URL": f"http://127.0.0.1:{port}",
            "ADMIN_TRUST_PROXY_HEADERS": "true",
            "ADMIN_TRUSTED_PROXY_IPS": "127.0.0.1",
            "ADMIN_TRUST_CLOUDFLARE_EDGE": "false",
            "UVICORN_FORWARDED_ALLOW_IPS": "127.0.0.1",
        }
    )
    proc = subprocess.Popen(
        [
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
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("uvicorn did not become ready")
    try:
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.integration
def test_uvicorn_health_uses_deployment_forwarded_allow_ips(
    uvicorn_server: str,
) -> None:
    payload = httpx.get(f"{uvicorn_server}/health", timeout=5.0).json()
    assert payload["admin_client_source"]["uvicorn_forwarded_allow_ips"] == "127.0.0.1"
    assert payload["admin_client_source"]["proxy_trust_enabled"] is True


@pytest.mark.integration
def test_uvicorn_integration_rotating_leftmost_spoof_shares_limiter_source(
    uvicorn_server: str,
) -> None:
    """POST login through uvicorn with deployment forwarded-allow-ips settings."""
    REAL_CLIENT = "198.51.100.10"
    session = httpx.Client(base_url=uvicorn_server, timeout=5.0, follow_redirects=False)
    statuses: list[int] = []
    for index in range(6):
        form = session.get("/admin/login")
        if form.status_code != 200:
            pytest.skip("admin login unavailable without database")
        match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
        if not match:
            pytest.skip("login form unavailable")
        csrf_token = match.group(1)
        response = session.post(
            "/admin/login",
            data={
                "username": "ghost",
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            headers={
                "X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}, 127.0.0.1",
            },
        )
        statuses.append(response.status_code)
    assert 429 in statuses
    assert statuses.count(429) >= 1


@pytest.mark.unit
def test_parse_trusted_proxy_networks_accepts_cidr_and_host() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8, 203.0.113.1")
    assert len(networks) == 2
