"""Unit tests for trusted-proxy client source resolution."""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest

from app import proxy_trust
from app.proxy_trust import SourceResolutionPath, resolve_client_source

RENDER_PROXY = "10.0.0.1"
CLOUDFLARE_EDGE = "172.64.0.10"
CLIENT_A = "203.0.113.50"
CLIENT_B = "203.0.113.77"
SPOOF = "203.0.113.99"
ATTACKER = "198.51.100.10"

TRUSTED = proxy_trust.parse_trusted_cidrs(f"{RENDER_PROXY}/32,10.0.0.0/8")
CF_NETWORKS = proxy_trust.parse_trusted_cidrs(f"{CLOUDFLARE_EDGE}/32")


def _resolve(
    *,
    peer: str | None = RENDER_PROXY,
    xff: str | None = None,
    forwarded: str | None = None,
    cf_ip: str | None = None,
    trust_cf: bool = True,
) -> proxy_trust.ClientSourceResolution:
    return resolve_client_source(
        immediate_peer=peer,
        x_forwarded_for=xff,
        forwarded=forwarded,
        cf_connecting_ip=cf_ip,
        trusted_networks=TRUSTED,
        cloudflare_networks=CF_NETWORKS,
        trust_cloudflare_edge=trust_cf,
    )


@pytest.mark.unit
def test_direct_spoof_single_value_xff_ignored() -> None:
    resolution = _resolve(peer=ATTACKER, xff=SPOOF)
    assert resolution.source == ATTACKER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_direct_spoof_multi_value_xff_ignored() -> None:
    resolution = _resolve(peer=ATTACKER, xff=f"{SPOOF}, {CLIENT_A}")
    assert resolution.source == ATTACKER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_cloudflare_append_ignores_attacker_leftmost() -> None:
    resolution = _resolve(
        xff=f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_EDGE}",
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_trusted_chain_resolves_expected_client() -> None:
    resolution = _resolve(xff=f"{CLIENT_A}, {CLOUDFLARE_EDGE}")
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_partial_trust_untrusted_intermediary_fails_closed() -> None:
    resolution = _resolve(
        peer=ATTACKER,
        xff=f"{SPOOF}, {RENDER_PROXY}",
    )
    assert resolution.source == ATTACKER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_direct_render_origin_ignores_cloudflare_header() -> None:
    resolution = _resolve(
        peer=ATTACKER,
        cf_ip=SPOOF,
        xff=None,
    )
    assert resolution.source == ATTACKER
    assert resolution.path == SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED


@pytest.mark.unit
def test_cf_connecting_ip_after_verified_cloudflare_hop() -> None:
    resolution = _resolve(
        xff=f"{SPOOF}, {CLIENT_A}, {CLOUDFLARE_EDGE}",
        cf_ip=CLIENT_A,
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.CLOUDFLARE_CONNECTING_IP


@pytest.mark.unit
def test_header_precedence_xff_over_forwarded() -> None:
    resolution = _resolve(
        xff=f"{CLIENT_A}, {CLOUDFLARE_EDGE}",
        forwarded=f'for="{CLIENT_B}"',
    )
    assert resolution.source == CLIENT_A
    assert resolution.path == SourceResolutionPath.TRUSTED_CHAIN_XFF


@pytest.mark.unit
def test_forwarded_header_used_when_xff_absent() -> None:
    resolution = _resolve(
        forwarded=f'for="{CLIENT_B}", for="{CLOUDFLARE_EDGE}"',
        xff=None,
    )
    assert resolution.source == CLIENT_B
    assert resolution.path == SourceResolutionPath.TRUSTED_CHAIN_FORWARDED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("203.0.113.1:8080", "203.0.113.1"),
        ("[2001:db8::1]:8080", "2001:db8::1"),
        (" 203.0.113.1 ", "203.0.113.1"),
    ],
)
def test_normalize_ip_address_formats(raw: str, expected: str) -> None:
    assert proxy_trust.normalize_ip_address(raw) == expected


@pytest.mark.unit
def test_malformed_and_empty_elements() -> None:
    assert _resolve(xff="not-an-ip, 10.0.0.2").path == SourceResolutionPath.MALFORMED_FORWARDING
    resolution = _resolve(xff=f" , , {CLIENT_A}, {CLOUDFLARE_EDGE}")
    assert resolution.source == CLIENT_A


@pytest.mark.unit
def test_invalid_address_in_chain_is_malformed() -> None:
    resolution = _resolve(xff=f"{CLIENT_A}, not-valid, {CLOUDFLARE_EDGE}")
    assert resolution.path == SourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_excessive_chain_length_is_malformed() -> None:
    hops = ", ".join(f"10.0.0.{index}" for index in range(1, 20))
    resolution = _resolve(xff=hops)
    assert resolution.path == SourceResolutionPath.MALFORMED_FORWARDING


@pytest.mark.unit
def test_missing_peer_returns_unknown() -> None:
    resolution = _resolve(peer=None)
    assert resolution.source == "unknown"
    assert resolution.path == SourceResolutionPath.MISSING_PEER


@pytest.mark.unit
def test_all_trusted_chain_falls_back_to_peer() -> None:
    resolution = _resolve(xff=f"{RENDER_PROXY}, 10.0.0.2")
    assert resolution.source == RENDER_PROXY
    assert resolution.path == SourceResolutionPath.ALL_TRUSTED_CHAIN


@pytest.mark.unit
def test_telemetry_logs_path_without_raw_addresses(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    resolution = _resolve(xff=f"{CLIENT_A}, {CLOUDFLARE_EDGE}")
    proxy_trust.log_source_resolution(resolution)
    assert CLIENT_A not in caplog.text
    assert CLOUDFLARE_EDGE not in caplog.text
    assert any(
        record.__dict__.get("source_resolution_path") == "trusted_chain_xff"
        for record in caplog.records
    )


@pytest.mark.unit
def test_invalid_forwarding_telemetry_is_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    proxy_trust.reset_source_resolution_telemetry_for_tests()
    caplog.set_level(logging.INFO, logger="app.proxy_trust")
    first = proxy_trust.ClientSourceResolution(
        source=ATTACKER,
        path=SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED,
    )
    second = proxy_trust.ClientSourceResolution(
        source=ATTACKER,
        path=SourceResolutionPath.MALFORMED_FORWARDING,
    )
    with patch.object(proxy_trust.time, "monotonic", side_effect=[100.0, 100.0, 161.0]):
        proxy_trust.log_source_resolution(first)
        proxy_trust.log_source_resolution(second)
        proxy_trust.log_source_resolution(second)
    assert caplog.text.count("Ignored or rejected forwarding headers") == 2
