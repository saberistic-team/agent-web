"""Tests for trusted-hop admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import ipaddress
from pathlib import Path
from typing import Generator

import httpx
import pytest
from fastapi import Request

from app import admin_auth
from app.config import get_settings
from app.proxy_trust import (
    SourceResolutionPath,
    normalize_client_address,
    parse_trusted_proxy_networks,
    parse_x_forwarded_for_chain,
    reset_proxy_trust_telemetry,
    resolve_admin_login_client_source,
    resolve_x_forwarded_for_client,
)

ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = ROOT / "render.yaml"
PROBE_APP = "tests.proxy_probe_app:app"

RENDER_TRUSTED_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
RENDER_LB_PEER = "10.0.0.55"
REAL_CLIENT = "203.0.113.50"
OTHER_CLIENT = "203.0.113.88"
SPOOFED_HOP = "203.0.113.99"


def _trusted_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxy_networks(
        tuple(spec.strip() for spec in RENDER_TRUSTED_IPS.split(",") if spec.strip())
    )


@pytest.fixture(autouse=True)
def proxy_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    reset_proxy_trust_telemetry()
    admin_auth.reset_login_rate_limiter()


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
    trust: bool,
    peer_host: str,
    headers: dict[str, str] | None = None,
    trusted_ips: str = RENDER_TRUSTED_IPS,
) -> tuple[str, SourceResolutionPath]:
    if trust:
        monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", trusted_ips)
    else:
        monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
        monkeypatch.delenv("ADMIN_TRUSTED_PROXY_IPS", raising=False)
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(peer_host=peer_host, headers=headers),
        settings,
    )
    return resolution.source, resolution.path


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=False,
        peer_host="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert source == "198.51.100.10"
    assert path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=False,
        peer_host="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.1, 203.0.113.2, 203.0.113.3"},
    )
    assert source == "198.51.100.10"
    assert path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_selects_real_client_not_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_HOP}, {REAL_CLIENT}"},
    )
    assert source == REAL_CLIENT
    assert path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB_PEER}"},
    )
    assert source == REAL_CLIENT
    assert path is SourceResolutionPath.TRUSTED_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host="198.51.100.10",
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {RENDER_LB_PEER}",
            "CF-Connecting-IP": REAL_CLIENT,
            "CF-Ray": "abc123",
        },
    )
    assert source == "198.51.100.10"
    assert path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_headers_without_ray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"CF-Connecting-IP": "203.0.113.77"},
    )
    assert source == RENDER_LB_PEER
    assert path is SourceResolutionPath.TRUSTED_PEER_FALLBACK


@pytest.mark.unit
def test_cf_connecting_ip_precedence_over_conflicting_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "CF-Ray": "7d1234abcdef-SEA",
            "X-Forwarded-For": f"{SPOOFED_HOP}, {OTHER_CLIENT}",
            "Forwarded": 'for="203.0.113.1"',
        },
    )
    assert source == REAL_CLIENT
    assert path is SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"Forwarded": f'for="{REAL_CLIENT}";proto=https'},
    )
    assert source == REAL_CLIENT
    assert path is SourceResolutionPath.FORWARDED_HEADER


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:443", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("  203.0.113.2  ", "203.0.113.2"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.999.999", None),
    ],
)
def test_normalize_client_address_formats(raw: str, expected: str | None) -> None:
    assert normalize_client_address(raw) == expected


@pytest.mark.unit
def test_xff_chain_whitespace_and_empty_elements() -> None:
    chain = parse_x_forwarded_for_chain(" 203.0.113.1 , , 10.0.0.1 ")
    assert chain == ["203.0.113.1", "10.0.0.1"]
    client = resolve_x_forwarded_for_client(
        chain,
        trusted_networks=_trusted_networks(),
    )
    assert client == "203.0.113.1"


@pytest.mark.unit
def test_overlong_xff_chain_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    long_chain = ", ".join(f"203.0.113.{index}" for index in range(40))
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"X-Forwarded-For": long_chain},
    )
    assert source == RENDER_LB_PEER
    assert path is SourceResolutionPath.INVALID_FORWARDED


@pytest.mark.unit
def test_invalid_xff_element_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    source, path = _resolve(
        monkeypatch,
        trust=True,
        peer_host=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"not-an-ip, {REAL_CLIENT}"},
    )
    assert source == RENDER_LB_PEER
    assert path is SourceResolutionPath.INVALID_FORWARDED


@pytest.mark.unit
def test_client_ip_delegates_to_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    settings = get_settings()
    request = _request(
        peer_host=RENDER_LB_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED_HOP}, {REAL_CLIENT}"},
    )
    assert admin_auth.client_ip(request, settings) == REAL_CLIENT


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.proxy_trust")
    settings = get_settings()
    request = _request(
        peer_host="198.51.100.10",
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    resolve_admin_login_client_source(request, settings)
    for record in caplog.records:
        message = record.getMessage()
        assert "203.0.113.99" not in message
        assert "198.51.100.10" not in message
        if hasattr(record, "source_resolution_path"):
            assert "203.0.113" not in str(record.source_resolution_path)


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration() -> None:
    content = RENDER_YAML.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in content
    assert "ADMIN_TRUST_PROXY_HEADERS" in content
    assert "ADMIN_TRUSTED_PROXY_IPS" in content
    assert "10.0.0.0/8" in content


@pytest.mark.unit
def test_verify_proxy_trust_config_script_passes() -> None:
    import verify_proxy_trust_config

    assert verify_proxy_trust_config.main() == 0


@pytest.mark.unit
@pytest.mark.integration
def test_rotating_spoofed_headers_share_one_limiter_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "3")
    store = FakeRateLimitStore()
    settings = get_settings()

    with shared_rate_limiter(store):
        for index in range(3):
            request = _request(
                peer_host=RENDER_LB_PEER,
                headers={"X-Forwarded-For": f"203.0.113.{index}, {REAL_CLIENT}"},
            )
            admission = admin_auth.try_admit_login_attempt(
                request, settings, username="ghost"
            )
            assert admission.admitted

        blocked_request = _request(
            peer_host=RENDER_LB_PEER,
            headers={"X-Forwarded-For": f"{SPOOFED_HOP}, {REAL_CLIENT}"},
        )
        blocked = admin_auth.try_admit_login_attempt(
            blocked_request, settings, username="ghost"
        )
        assert blocked.throttled

    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert len(store.rows) == 1
    assert source_key in store.rows


def _wait_for_port(port: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"uvicorn did not listen on port {port}")


@pytest.fixture
def uvicorn_probe_server(monkeypatch: pytest.MonkeyPatch) -> Generator[int, None, None]:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    command = [
        "uvicorn",
        PROBE_APP,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-proxy-headers",
    ]
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_uvicorn_no_proxy_headers_integration(uvicorn_probe_server: int) -> None:
    """Exercise the same uvicorn flag used in render.yaml startCommand."""
    render_command = RENDER_YAML.read_text(encoding="utf-8")
    assert "--no-proxy-headers" in render_command

    with httpx.Client(timeout=5.0) as client:
        response = client.get(
            f"http://127.0.0.1:{uvicorn_probe_server}/source",
            headers={"X-Forwarded-For": f"{SPOOFED_HOP}, {REAL_CLIENT}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["source"] == REAL_CLIENT
        assert payload["path"] == SourceResolutionPath.TRUSTED_XFF.value


@pytest.mark.unit
def test_limiter_rows_store_digests_not_raw_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", RENDER_TRUSTED_IPS)
    store = FakeRateLimitStore()
    settings = get_settings()
    with shared_rate_limiter(store):
        request = _request(
            peer_host=RENDER_LB_PEER,
            headers={"X-Forwarded-For": f"203.0.113.99, {REAL_CLIENT}"},
        )
        admin_auth.try_admit_login_attempt(request, settings, username="ghost")

    for key in store.rows:
        assert REAL_CLIENT not in key
        assert "203.0.113.99" not in key
        assert len(key) == 64
