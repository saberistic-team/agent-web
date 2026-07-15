"""Tests for trusted-proxy admin login client source resolution (#239)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    ClientSourceResolutionPath,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_LB = "10.15.0.4"
REAL_CLIENT = "203.0.113.77"
OTHER_CLIENT = "203.0.113.88"
CLOUDFLARE_EDGE = "104.16.132.229"
ATTACKER_SPOOF = "198.18.0.50"


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Any:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_CIDRS",
        overrides.get("trusted_peer_cidrs", "10.0.0.0/8,127.0.0.1,::1"),
    )
    if "hop_cidrs" in overrides:
        monkeypatch.setenv("ADMIN_FORWARDED_TRUSTED_HOP_CIDRS", overrides["hop_cidrs"])
    else:
        monkeypatch.delenv("ADMIN_FORWARDED_TRUSTED_HOP_CIDRS", raising=False)
    if "max_hops" in overrides:
        monkeypatch.setenv("ADMIN_FORWARDED_MAX_HOPS", overrides["max_hops"])
    reset_client_source_telemetry_for_tests()
    return get_settings()


def _request(
    peer: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "headers": headers or [],
        "client": (peer, 54321),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode("ascii"), value.encode("ascii"))


@pytest.fixture(autouse=True)
def _reset_telemetry() -> Iterator[None]:
    reset_client_source_telemetry_for_tests()
    yield
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    for header_value in (
        ATTACKER_SPOOF,
        f"{ATTACKER_SPOOF}, {OTHER_CLIENT}",
    ):
        request = _request(
            "203.0.113.10",
            [_header("x-forwarded-for", header_value)],
        )
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == "203.0.113.10"
        assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER
        assert resolution.forwarding_rejected is True


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header(
                "x-forwarded-for",
                f"{ATTACKER_SPOOF}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_partial_trust_interior_trusted_hop_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        hop_cidrs="10.0.0.0/8,127.0.0.1",
    )
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"10.0.0.2, {REAL_CLIENT}, 10.0.0.1")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is ClientSourceResolutionPath.PEER_FALLBACK


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("cf-connecting-ip", REAL_CLIENT),
            _header("x-forwarded-for", ATTACKER_SPOOF),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_SPOOF
    assert resolution.path is ClientSourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_cf_connecting_ip_used_only_with_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("cf-connecting-ip", REAL_CLIENT),
            _header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_before_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"),
            _header("forwarded", f"for={OTHER_CLIENT};proto=https"),
            _header("cf-connecting-ip", ATTACKER_SPOOF),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.XFF_TRUSTED_CHAIN


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header(
                "forwarded",
                f"for={REAL_CLIENT};proto=https, for={CLOUDFLARE_EDGE};proto=https",
            )
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.FORWARDED_TRUSTED_CHAIN


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        (" 203.0.113.2 , 10.0.0.1 ", "203.0.113.2"),
    ],
)
def test_address_normalization_variants(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, [_header("x-forwarded-for", raw)])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == expected


@pytest.mark.unit
def test_malformed_and_overlong_chains_fail_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, max_hops="3")
    malformed = _request(RENDER_LB, [_header("x-forwarded-for", "not-an-ip")])
    assert resolve_admin_login_client_source(malformed, settings).source == RENDER_LB

    overlong = _request(
        RENDER_LB,
        [_header("x-forwarded-for", "203.0.113.1, 203.0.113.2, 203.0.113.3, 203.0.113.4")],
    )
    assert resolve_admin_login_client_source(overlong, settings).source == RENDER_LB

    empty_element = _request(RENDER_LB, [_header("x-forwarded-for", "203.0.113.1,,10.0.0.2")])
    assert resolve_admin_login_client_source(empty_element, settings).source == RENDER_LB


@pytest.mark.unit
def test_proxy_trust_disabled_ignores_all_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request(
        RENDER_LB,
        [
            _header("x-forwarded-for", REAL_CLIENT),
            _header("cf-connecting-ip", REAL_CLIENT),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.path is ClientSourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_telemetry_contains_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        "203.0.113.10",
        [_header("x-forwarded-for", ATTACKER_SPOOF)],
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    combined = caplog.text
    assert ATTACKER_SPOOF not in combined
    assert "xff_trusted_chain" not in combined
    assert any(
        record.__dict__.get("forwarding_rejected") is True
        for record in caplog.records
        if record.name == "app.admin_client_source"
    )


@pytest.mark.unit
def test_limiter_keys_use_digest_not_raw_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source_key = admin_auth.build_source_rate_limit_key(REAL_CLIENT)
    assert REAL_CLIENT not in source_key
    assert len(source_key) == 64
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}")],
    )
    resolved = resolve_admin_login_client_source(request, settings).source
    assert admin_auth.build_source_rate_limit_key(resolved) == source_key


@pytest.mark.unit
def test_unknown_peer_when_client_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    request = Request(
        {
            "type": "http",
            "headers": [],
            "method": "POST",
            "path": "/admin/login",
        }
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is ClientSourceResolutionPath.UNKNOWN_PEER


@pytest.mark.unit
def test_malformed_forwarded_header_fails_closed_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, [_header("forwarded", "for=not-an-ip;proto=https")])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.reject_reason == "malformed_forwarded"


@pytest.mark.unit
def test_cf_connecting_ip_used_when_xff_has_only_cloudflare_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("x-forwarded-for", CLOUDFLARE_EDGE),
            _header("cf-connecting-ip", REAL_CLIENT),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is ClientSourceResolutionPath.CF_CONNECTING_IP_VERIFIED


@pytest.mark.unit
def test_malformed_cf_connecting_ip_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"),
            _header("cf-connecting-ip", "not-an-ip"),
        ],
    )
    # XFF resolves first; CF path not reached when XFF yields client
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_non_ip_immediate_peer_host_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request("testclient", [_header("x-forwarded-for", REAL_CLIENT)])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "testclient"


@pytest.mark.unit
def test_sampled_forwarding_telemetry_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        "203.0.113.10",
        [_header("x-forwarded-for", ATTACKER_SPOOF)],
    )
    with caplog.at_level(logging.INFO):
        for _ in range(25):
            resolve_admin_login_client_source(request, settings)
    info_events = [
        record
        for record in caplog.records
        if record.name == "app.admin_client_source"
        and record.getMessage() == "Admin login forwarding header telemetry"
    ]
    assert len(info_events) == 20


@pytest.mark.unit
def test_invalid_configured_cidr_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, trusted_peer_cidrs="not-a-cidr,10.0.0.0/8")
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}")],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT


@pytest.mark.unit
def test_malformed_cf_connecting_ip_when_xff_all_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [
            _header("x-forwarded-for", CLOUDFLARE_EDGE),
            _header("cf-connecting-ip", "not-an-ip"),
        ],
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.reject_reason == "malformed_cf_connecting_ip"


@pytest.mark.unit
def test_forwarded_for_unknown_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, [_header("forwarded", "for=unknown;proto=https")])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.reject_reason == "malformed_forwarded"


@pytest.mark.unit
def test_resolution_debug_telemetry_emitted_for_trusted_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}")],
    )
    with caplog.at_level(logging.DEBUG):
        resolve_admin_login_client_source(request, settings)
    assert any(
        record.__dict__.get("client_source_resolution") == "xff_trusted_chain"
        for record in caplog.records
        if record.name == "app.admin_client_source"
    )


@pytest.mark.unit
def test_normalize_rejects_invalid_ipv6_bracket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(RENDER_LB, [_header("x-forwarded-for", "[2001:db8::1")])
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == RENDER_LB
    assert resolution.reject_reason == "malformed_x_forwarded_for"


@pytest.mark.unit
def test_peer_fallback_records_ignored_forwarding_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(monkeypatch)
    request = _request(
        RENDER_LB,
        [_header("x-forwarded-for", f"{CLOUDFLARE_EDGE}, {RENDER_LB}")],
    )
    with caplog.at_level(logging.INFO):
        resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.path is ClientSourceResolutionPath.PEER_FALLBACK
    assert any(
        record.__dict__.get("forwarding_event") == "forwarding_ignored_no_client"
        for record in caplog.records
        if record.name == "app.admin_client_source"
    )


@pytest.mark.unit
def test_limiter_rotating_spoof_does_not_create_multiple_source_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    keys: set[str] = set()
    for index in range(5):
        request = _request(
            "203.0.113.10",
            [_header("x-forwarded-for", f"203.0.113.{index}")],
        )
        source = resolve_admin_login_client_source(request, settings).source
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert keys == {admin_auth.build_source_rate_limit_key("203.0.113.10")}


@pytest.mark.integration
def test_uvicorn_forwarded_allow_ips_rejects_spoofed_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$test")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "127.0.0.1,::1")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        forwarded_allow_ips="127.0.0.1",
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if health.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("uvicorn server failed to start")

    assert config.forwarded_allow_ips == "127.0.0.1"

    settings = get_settings()
    scope = {
        "type": "http",
        "headers": [
            _header("x-forwarded-for", f"{ATTACKER_SPOOF}, {REAL_CLIENT}"),
        ],
        "client": ("127.0.0.1", 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    resolution = resolve_admin_login_client_source(Request(scope), settings)
    assert resolution.source == REAL_CLIENT

    spoof_scope = dict(scope)
    spoof_scope["client"] = ("203.0.113.250", 12345)
    spoofed = resolve_admin_login_client_source(Request(spoof_scope), settings)
    assert spoofed.source == "203.0.113.250"
    assert spoofed.forwarding_rejected is True

    server.should_exit = True
    thread.join(timeout=3)
