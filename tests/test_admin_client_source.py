"""Tests for verified admin login client-source resolution."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Request

from app.admin_client_source import (
    ClientSourceResolution,
    normalize_client_source,
    reset_client_source_telemetry_for_tests,
    resolve_admin_login_client_source,
)
from app.config import get_settings
from app.proxy_trust import parse_trusted_proxy_networks

RENDER_PROXY = "10.0.0.2"
REAL_CLIENT = "203.0.113.55"
ATTACKER_SPOOF = "203.0.113.99"
CLOUDFLARE_EDGE = "172.18.0.1"


def _request(
    *,
    peer: str | None = "198.51.100.10",
    immediate_peer: str | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    header_list = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "headers": header_list,
        "client": (peer, 12345) if peer is not None else None,
        "method": "POST",
        "path": "/admin/login",
    }
    if immediate_peer is not None:
        scope["immediate_peer"] = immediate_peer
    elif peer is not None:
        scope["immediate_peer"] = peer
    return Request(scope)


def _proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv(
        "ADMIN_TRUSTED_PROXY_IPS",
        "10.0.0.0/8,172.16.0.0/12,127.0.0.0/8,::1/128",
    )
    reset_client_source_telemetry_for_tests()


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    reset_client_source_telemetry_for_tests()


@pytest.mark.unit
def test_normalize_client_source_formats() -> None:
    assert normalize_client_source("203.0.113.1") == "203.0.113.1"
    assert normalize_client_source(" 2001:db8::1 ") == "2001:db8::1"
    assert normalize_client_source("::ffff:203.0.113.7") == "203.0.113.7"
    assert normalize_client_source("[2001:db8::5]:443") == "2001:db8::5"
    assert normalize_client_source("203.0.113.8:8080") == "203.0.113.8"
    assert normalize_client_source("") is None
    assert normalize_client_source("not-an-ip") is None


@pytest.mark.unit
def test_parse_trusted_proxy_networks_skips_invalid_entries() -> None:
    networks = parse_trusted_proxy_networks("10.0.0.0/8,bogus,203.0.113.1")
    assert len(networks) == 2


@pytest.mark.unit
def test_direct_spoof_single_and_multi_xff_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    direct_peer = "198.51.100.10"

    single = _request(
        peer=direct_peer,
        headers={"X-Forwarded-For": ATTACKER_SPOOF},
    )
    assert resolve_admin_login_client_source(single, settings) == ClientSourceResolution(
        source=direct_peer,
        path="untrusted_peer_headers_ignored",
    )

    multi = _request(
        peer=direct_peer,
        headers={"X-Forwarded-For": f"{ATTACKER_SPOOF}, 203.0.113.1"},
    )
    assert resolve_admin_login_client_source(multi, settings).source == direct_peer


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": f"{ATTACKER_SPOOF}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}",
        },
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == REAL_CLIENT
    assert result.path == "verified_forwarded_chain"


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    assert resolve_admin_login_client_source(request, settings).source == REAL_CLIENT


@pytest.mark.unit
def test_partial_trust_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    untrusted_hop = "203.0.113.44"
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {untrusted_hop}"},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == untrusted_hop
    assert result.path == "verified_forwarded_chain"


@pytest.mark.unit
def test_direct_render_origin_ignores_cf_connecting_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    direct_peer = "198.51.100.20"
    request = _request(
        peer=direct_peer,
        headers={"CF-Connecting-IP": REAL_CLIENT},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == direct_peer
    assert result.path == "untrusted_peer_headers_ignored"


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded_and_cf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={
            "X-Forwarded-For": REAL_CLIENT,
            "Forwarded": 'for="203.0.113.1"',
            "CF-Connecting-IP": "203.0.113.2",
        },
    )
    assert resolve_admin_login_client_source(request, settings).source == REAL_CLIENT


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"Forwarded": f'for="{REAL_CLIENT}"'},
    )
    assert resolve_admin_login_client_source(request, settings).source == REAL_CLIENT


@pytest.mark.unit
def test_cf_connecting_ip_ignored_without_forwarding_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"CF-Connecting-IP": REAL_CLIENT},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == RENDER_PROXY
    assert result.path == "cf_connecting_ip_ignored_without_chain"


@pytest.mark.unit
def test_invalid_and_overlong_forwarding_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    invalid = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    assert resolve_admin_login_client_source(invalid, settings).path == "invalid_forwarding_data"

    overlong = ",".join(f"203.0.113.{index}" for index in range(40))
    too_long = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": overlong},
    )
    assert resolve_admin_login_client_source(too_long, settings).path == "invalid_forwarding_data"


@pytest.mark.unit
def test_missing_peer_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(peer=None, headers={"X-Forwarded-For": REAL_CLIENT})
    assert resolve_admin_login_client_source(request, settings).source == "unknown"


@pytest.mark.unit
def test_proxy_trust_disabled_uses_peer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    settings = get_settings()
    request = _request(
        peer="198.51.100.10",
        headers={"X-Forwarded-For": ATTACKER_SPOOF},
    )
    result = resolve_admin_login_client_source(request, settings)
    assert result.source == "198.51.100.10"
    assert result.path == "direct_peer_no_trust"


@pytest.mark.unit
def test_untrusted_forwarding_telemetry_has_no_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.INFO)
    request = _request(
        peer="198.51.100.10",
        headers={
            "X-Forwarded-For": ATTACKER_SPOOF,
            "CF-Connecting-IP": REAL_CLIENT,
        },
    )
    with patch("app.admin_client_source.time.monotonic", return_value=1000.0):
        resolve_admin_login_client_source(request, settings)
    assert "203.0.113" not in caplog.text
    assert any(
        record.message == "Admin login ignored forwarding headers from untrusted peer"
        for record in caplog.records
    )


@pytest.mark.unit
def test_resolution_debug_telemetry_omits_raw_addresses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    caplog.set_level(logging.DEBUG)
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": REAL_CLIENT},
    )
    resolve_admin_login_client_source(request, settings)
    assert REAL_CLIENT not in caplog.text
    assert any(
        record.message == "Admin login client source resolved"
        for record in caplog.records
    )


@pytest.mark.unit
def test_duplicate_forwarding_elements_resolve_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"{REAL_CLIENT}, {REAL_CLIENT}, {CLOUDFLARE_EDGE}"},
    )
    assert resolve_admin_login_client_source(request, settings).source == REAL_CLIENT


@pytest.mark.unit
def test_whitespace_and_empty_forwarding_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proxy_env(monkeypatch)
    settings = get_settings()
    request = _request(
        peer=RENDER_PROXY,
        immediate_peer=RENDER_PROXY,
        headers={"X-Forwarded-For": f"  {REAL_CLIENT}  , , {CLOUDFLARE_EDGE} "},
    )
    assert resolve_admin_login_client_source(request, settings).source == REAL_CLIENT
