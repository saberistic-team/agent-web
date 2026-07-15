"""Tests for trusted-proxy admin login source resolution (#239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import Request

from app import admin_auth
from app.config import get_settings
from app.proxy_trust import (
    SourceResolutionPath,
    normalize_ip_address,
    reset_source_resolution_telemetry,
    resolve_admin_login_client_source,
    resolve_admin_login_client_source_detail,
    source_resolution_telemetry_snapshot,
)

RENDER_PROXY = "10.0.0.1"
CF_EDGE = "104.16.0.1"
CLIENT = "203.0.113.50"
SPOOFED = "198.18.0.99"
DIRECT_PEER = "198.51.100.10"
UNTRUSTED_INTERMEDIARY = "203.0.113.9"


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


def _trusted_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", "104.16.0.0/13")
    monkeypatch.delenv("ADMIN_TRUST_CLOUDFLARE_EDGE", raising=False)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_source_resolution_telemetry()


@pytest.mark.unit
def test_normalize_ip_address_formats() -> None:
    assert normalize_ip_address("203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("203.0.113.1:443") == "203.0.113.1"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_ip_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert normalize_ip_address("  203.0.113.1  ") == "203.0.113.1"
    assert normalize_ip_address("") is None
    assert normalize_ip_address("not-an-ip") is None


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()

    single = _request_with_client(
        DIRECT_PEER,
        headers={"X-Forwarded-For": SPOOFED},
    )
    multi = _request_with_client(
        DIRECT_PEER,
        headers={"X-Forwarded-For": f"{SPOOFED}, {CLIENT}, {CF_EDGE}"},
    )
    assert resolve_admin_login_client_source(single, settings) == DIRECT_PEER
    assert resolve_admin_login_client_source(multi, settings) == DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{SPOOFED}, {CLIENT}, {CF_EDGE}",
            "CF-Connecting-IP": CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == SourceResolutionPath.CF_CONNECTING_IP


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {CF_EDGE}"},
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_WALK


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {UNTRUSTED_INTERMEDIARY}, {CF_EDGE}"},
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == UNTRUSTED_INTERMEDIARY
    assert resolution.path == SourceResolutionPath.XFF_TRUSTED_WALK


@pytest.mark.unit
def test_direct_render_origin_ignores_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        DIRECT_PEER,
        headers={
            "CF-Connecting-IP": CLIENT,
            "X-Forwarded-For": f"{CLIENT}, {CF_EDGE}",
            "Forwarded": f'for="{CLIENT}";proto=https, for="{CF_EDGE}"',
        },
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == DIRECT_PEER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_PEER


@pytest.mark.unit
def test_conflicting_header_families_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{CLIENT}, {CF_EDGE}",
            "Forwarded": f'for="{SPOOFED}";proto=https, for="{CF_EDGE}"',
        },
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={
            "Forwarded": f'for="{CLIENT}";proto=https, for="{CF_EDGE}";proto=https',
        },
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == CLIENT
    assert resolution.path == SourceResolutionPath.FORWARDED_TRUSTED_WALK


@pytest.mark.unit
def test_address_format_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()

    empty = _request_with_client(RENDER_PROXY, headers={"X-Forwarded-For": ""})
    assert resolve_admin_login_client_source(empty, settings) == "unknown"

    invalid = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip, 104.16.0.1"},
    )
    assert resolve_admin_login_client_source(invalid, settings) == "unknown"

    mapped = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"::ffff:{CLIENT}, {CF_EDGE}"},
    )
    assert resolve_admin_login_client_source(mapped, settings) == CLIENT


@pytest.mark.unit
def test_excessive_forward_chain_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    hops = [f"203.0.113.{index}" for index in range(12)]
    hops.extend([CLIENT, CF_EDGE])
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": ", ".join(hops)},
    )
    resolution = resolve_admin_login_client_source_detail(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.INVALID_FORWARDING


@pytest.mark.unit
def test_telemetry_records_path_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _trusted_proxy_env(monkeypatch)
    settings = get_settings()
    request = _request_with_client(
        RENDER_PROXY,
        headers={"X-Forwarded-For": f"{CLIENT}, {CF_EDGE}"},
    )
    resolve_admin_login_client_source(request, settings)
    snapshot = source_resolution_telemetry_snapshot()
    assert snapshot.get("xff_trusted_walk") == 1
    for message in caplog.messages:
        assert CLIENT not in message
        assert CF_EDGE not in message


@pytest.mark.unit
def test_limiter_keys_do_not_persist_raw_addresses() -> None:
    client_ip = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(client_ip)
    assert client_ip not in source_key
    assert len(source_key) == 64
    assert all(char in "0123456789abcdef" for char in source_key)


@pytest.mark.unit
def test_render_yaml_proxy_trust_configuration_is_consistent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")

    start_match = re.search(
        r"startCommand:\s*uvicorn app\.main:app.*--forwarded-allow-ips='([^']+)'",
        render_yaml,
    )
    assert start_match is not None
    start_cidrs = start_match.group(1)

    env_cidrs = re.search(
        r"key: ADMIN_TRUSTED_PROXY_CIDRS\n\s+value: \"([^\"]+)\"",
        render_yaml,
    )
    uvicorn_env = re.search(
        r"key: UVICORN_FORWARDED_ALLOW_IPS\n\s+value: \"([^\"]+)\"",
        render_yaml,
    )
    trust_flag = re.search(
        r"key: ADMIN_TRUST_PROXY_HEADERS\n\s+value: \"true\"",
        render_yaml,
    )
    edge_flag = re.search(
        r"key: ADMIN_TRUST_CLOUDFLARE_EDGE\n\s+value: \"true\"",
        render_yaml,
    )

    assert env_cidrs is not None
    assert uvicorn_env is not None
    assert trust_flag is not None
    assert edge_flag is not None
    assert env_cidrs.group(1) == start_cidrs
    assert uvicorn_env.group(1) == start_cidrs
