"""Unit tests for trusted-hop admin login client source resolution."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest
from fastapi import Request

from app import admin_auth
from app.config import get_settings
from app.proxy_trust import (
    SourceResolutionPath,
    parse_client_address,
    resolve_admin_login_client_source,
    resolve_right_to_left_client,
)

RENDER_LB = "10.0.0.1"
CLOUDFLARE_EDGE = "198.41.128.10"
REAL_CLIENT = "203.0.113.50"
ATTACKER_PEER = "198.51.100.10"
SPOOFED_LEFTMOST = "203.0.113.99"

TRUSTED_CIDRS = "127.0.0.1,10.0.0.0/8,198.41.128.0/17"


def _request(
    *,
    peer: str | None,
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "x")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return get_settings()


@pytest.mark.unit
def test_parse_client_address_normalizes_ipv4_ports_and_mapped_ipv6() -> None:
    assert parse_client_address("203.0.113.1:443") == "203.0.113.1"
    assert parse_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert parse_client_address("::ffff:203.0.113.1") == "203.0.113.1"
    assert parse_client_address("  2001:0db8::1  ") == "2001:db8::1"


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, ADMIN_TRUST_PROXY_HEADERS="false")
    for header in (
        SPOOFED_LEFTMOST,
        f"{SPOOFED_LEFTMOST}, {REAL_CLIENT}",
    ):
        request = _request(peer=ATTACKER_PEER, headers={"X-Forwarded-For": header})
        resolution = resolve_admin_login_client_source(request, settings)
        assert resolution.source == ATTACKER_PEER
        assert resolution.path is SourceResolutionPath.DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_behavior_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    xff = f"{SPOOFED_LEFTMOST}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.XFF_RIGHT_TO_LEFT


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    xff = f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.XFF_RIGHT_TO_LEFT


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS="10.0.0.0/8",
    )
    # Trusted Render peer, but only untrusted intermediary in chain before client.
    xff = f"{REAL_CLIENT}, 203.0.113.250"
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": xff})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "203.0.113.250"
    assert resolution.path is SourceResolutionPath.XFF_RIGHT_TO_LEFT


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_vendor_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    request = _request(
        peer=ATTACKER_PEER,
        headers={
            "CF-Connecting-IP": REAL_CLIENT,
            "X-Forwarded-For": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == ATTACKER_PEER
    assert resolution.path is SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}",
            "Forwarded": f'for="{SPOOFED_LEFTMOST}";proto=https',
            "CF-Connecting-IP": SPOOFED_LEFTMOST,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.XFF_RIGHT_TO_LEFT


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    request = _request(
        peer=RENDER_LB,
        headers={"Forwarded": f'for={REAL_CLIENT};proto=https, for={CLOUDFLARE_EDGE}'},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.FORWARDED_RFC7239


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::9", "2001:db8::9"),
        ("::ffff:203.0.113.2", "203.0.113.2"),
        ("not-an-ip", None),
        ("", None),
    ],
)
def test_address_format_parsing(raw: str, expected: str | None) -> None:
    assert parse_client_address(raw) == expected


@pytest.mark.unit
def test_overlong_and_empty_xff_elements_resolve_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    overlong = ", ".join([f"203.0.113.{i % 250}" for i in range(40)])
    request = _request(peer=RENDER_LB, headers={"X-Forwarded-For": overlong})
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == "unknown"
    assert resolution.path is SourceResolutionPath.MALFORMED_CONSERVATIVE

    request_empty = _request(peer=RENDER_LB, headers={"X-Forwarded-For": " , , "})
    resolution_empty = resolve_admin_login_client_source(request_empty, settings)
    assert resolution_empty.source == "unknown"


@pytest.mark.unit
def test_right_to_left_helper_duplicate_and_whitespace_elements() -> None:
    from app.proxy_trust import parse_trusted_proxy_networks

    trusted = parse_trusted_proxy_networks(TRUSTED_CIDRS.split(","))
    chain = [f"  {REAL_CLIENT}  ", CLOUDFLARE_EDGE, REAL_CLIENT]
    assert (
        resolve_right_to_left_client(chain, peer=RENDER_LB, trusted_networks=trusted)
        == REAL_CLIENT
    )


@pytest.mark.unit
def test_cf_connecting_ip_only_with_verified_cloudflare_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    request = _request(
        peer=RENDER_LB,
        headers={
            "X-Forwarded-For": CLOUDFLARE_EDGE,
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert resolution.source == REAL_CLIENT
    assert resolution.path is SourceResolutionPath.CF_CONNECTING_IP_VERIFIED


@pytest.mark.unit
def test_privacy_telemetry_and_limiter_state_exclude_raw_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    caplog.set_level(logging.INFO)
    request = _request(
        peer=ATTACKER_PEER,
        headers={"X-Forwarded-For": SPOOFED_LEFTMOST},
    )
    resolution = resolve_admin_login_client_source(request, settings)
    assert SPOOFED_LEFTMOST not in caplog.text
    assert ATTACKER_PEER not in caplog.text
    assert "x-forwarded-for" not in caplog.text.lower()

    source_key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert SPOOFED_LEFTMOST not in source_key
    assert len(source_key) == 64
    assert source_key == hashlib.sha256(f"src:{resolution.source.lower()}".encode()).hexdigest()


@pytest.mark.unit
def test_rotating_spoofed_leftmost_produces_stable_limiter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        ADMIN_TRUST_PROXY_HEADERS="true",
        ADMIN_TRUSTED_PROXY_CIDRS=TRUSTED_CIDRS,
    )
    keys: set[str] = set()
    for spoof in (SPOOFED_LEFTMOST, "203.0.113.1", "203.0.113.2"):
        request = _request(
            peer=RENDER_LB,
            headers={"X-Forwarded-For": f"{spoof}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
        )
        source = resolve_admin_login_client_source(request, settings).source
        keys.add(admin_auth.build_source_rate_limit_key(source))
    assert keys == {admin_auth.build_source_rate_limit_key(REAL_CLIENT)}
