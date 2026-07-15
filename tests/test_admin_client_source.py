"""Unit tests for trusted-proxy admin login client-source resolution (#239)."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request

from app import admin_auth
from app.admin_client_source import (
    PATH_CF_CONNECTING_IP,
    PATH_DIRECT_PEER,
    PATH_FORWARDED_CHAIN,
    PATH_FORWARDED_RFC7239,
    PATH_INVALID_FORWARDED,
    PATH_UNTRUSTED_FORWARDED,
    client_source_telemetry_snapshot,
    reset_client_source_telemetry,
    resolve_admin_login_client_source,
)
from app.config import get_settings

RENDER_LB = "10.0.0.1"
CF_EDGE = "172.64.0.1"
CLIENT_A = "203.0.113.10"
CLIENT_B = "203.0.113.20"
SPOOFED = "203.0.113.99"
UNTRUSTED_PEER = "198.51.100.50"


def _request(
    client_host: str,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": header_list,
        "client": (client_host, 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry()


@pytest.mark.unit
def test_direct_spoof_single_and_multi_value_xff_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    for header in (
        SPOOFED,
        f"{SPOOFED}, {CLIENT_A}",
        f"{SPOOFED}, {CLIENT_A}, {RENDER_LB}",
    ):
        resolution = resolve_admin_login_client_source(
            _request(UNTRUSTED_PEER, {"X-Forwarded-For": header}),
            settings,
        )
        assert resolution.source == UNTRUSTED_PEER
        assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {"X-Forwarded-For": f"{SPOOFED}, {CLIENT_A}, {RENDER_LB}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {"X-Forwarded-For": f"{CLIENT_A}, {CF_EDGE}, {RENDER_LB}"},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_partial_trust_untrusted_peer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            UNTRUSTED_PEER,
            {
                "X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}",
                "CF-Connecting-IP": CLIENT_A,
            },
        ),
        settings,
    )
    assert resolution.source == UNTRUSTED_PEER
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_direct_render_origin_ignores_spoofed_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {"CF-Connecting-IP": CLIENT_A},
        ),
        settings,
    )
    assert resolution.source == RENDER_LB
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_header_precedence_xff_before_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {
                "X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}",
                "Forwarded": f'for="{CLIENT_B}"',
                "CF-Connecting-IP": CLIENT_B,
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_FORWARDED_CHAIN


@pytest.mark.unit
def test_forwarded_rfc7239_used_when_xff_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {"Forwarded": f'for="{CLIENT_A}", for="{RENDER_LB}"'},
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_FORWARDED_RFC7239


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"{CLIENT_A}, {RENDER_LB}", CLIENT_A),
        (f"2001:db8::1, {RENDER_LB}", "2001:db8::1"),
        (f"::ffff:203.0.113.5, {RENDER_LB}", "203.0.113.5"),
        (f"  {CLIENT_A}  , {RENDER_LB}", CLIENT_A),
    ],
)
def test_address_formats_normalized(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    expected: str,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(RENDER_LB, {"X-Forwarded-For": header}),
        settings,
    )
    assert resolution.source == expected


@pytest.mark.unit
def test_invalid_and_overlong_forwarded_chains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    invalid = resolve_admin_login_client_source(
        _request(RENDER_LB, {"X-Forwarded-For": f"not-an-ip, {RENDER_LB}"}),
        settings,
    )
    assert invalid.source == RENDER_LB
    assert invalid.path == PATH_INVALID_FORWARDED

    overlong = ", ".join([f"203.0.113.{index}" for index in range(40)] + [RENDER_LB])
    too_long = resolve_admin_login_client_source(
        _request(RENDER_LB, {"X-Forwarded-For": overlong}),
        settings,
    )
    assert too_long.source == RENDER_LB
    assert too_long.path == PATH_INVALID_FORWARDED


@pytest.mark.unit
def test_cf_connecting_ip_requires_edge_hop_in_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {
                "X-Forwarded-For": f"{CF_EDGE}, {RENDER_LB}",
                "CF-Connecting-IP": CLIENT_A,
            },
        ),
        settings,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == PATH_CF_CONNECTING_IP


@pytest.mark.unit
def test_telemetry_records_paths_without_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    caplog.set_level(logging.INFO)
    resolve_admin_login_client_source(
        _request(UNTRUSTED_PEER, {"X-Forwarded-For": SPOOFED}),
        settings,
    )
    snapshot = client_source_telemetry_snapshot()
    assert PATH_UNTRUSTED_FORWARDED in snapshot or PATH_DIRECT_PEER in snapshot
    for record in caplog.records:
        message = record.getMessage()
        assert SPOOFED not in message
        assert UNTRUSTED_PEER not in message


@pytest.mark.unit
def test_limiter_key_uses_resolved_source_not_spoofed_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {"X-Forwarded-For": f"{SPOOFED}, {CLIENT_A}, {RENDER_LB}"},
        ),
        settings,
    )
    key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert key == admin_auth.build_source_rate_limit_key(CLIENT_A)
    assert key != admin_auth.build_source_rate_limit_key(SPOOFED)


@pytest.mark.unit
def test_trust_disabled_ignores_all_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(
            RENDER_LB,
            {
                "X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}",
                "CF-Connecting-IP": CLIENT_B,
            },
        ),
        settings,
    )
    assert resolution.source == RENDER_LB
    assert resolution.path == PATH_DIRECT_PEER


@pytest.mark.unit
def test_limiter_rows_store_only_digests_not_raw_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    settings = get_settings()
    resolution = resolve_admin_login_client_source(
        _request(RENDER_LB, {"X-Forwarded-For": f"{CLIENT_A}, {RENDER_LB}"}),
        settings,
    )
    key = admin_auth.build_source_rate_limit_key(resolution.source)
    assert CLIENT_A not in key
    assert RENDER_LB not in key
    assert len(key) == 64

