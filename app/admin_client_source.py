"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDED_SAMPLE_INTERVAL_SECONDS = 60.0
_INVALID_FORWARDED_SAMPLE_CAP = 8

# RFC 7239 Forwarded: for=203.0.113.1 or for="[2001:db8::1]"
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"?([^;,\s"]+)"?)',
    re.IGNORECASE,
)

_last_invalid_forwarded_log_at = 0.0
_invalid_forwarded_log_count = 0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    UNTRUSTED_HEADERS_REJECTED = "untrusted_headers_rejected"
    MALFORMED_FORWARDING = "malformed_forwarding"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: SourceResolutionPath


def parse_proxy_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured CIDR strings; invalid entries are ignored."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; strip ports when present."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate.partition("]")
        inner = host[1:].strip()
        if remainder.startswith(":"):
            candidate = inner
        else:
            candidate = inner

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def address_in_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarded_chain(header_value: str) -> list[str] | None:
    parts = [segment.strip() for segment in header_value.split(",")]
    elements = [part for part in parts if part]
    if not elements:
        return []
    if len(elements) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return elements


def _resolve_from_forwarded_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> str | None:
    """Walk X-Forwarded-For right-to-left, stripping trusted proxy hops."""
    normalized: list[str] = []
    for entry in chain:
        address = normalize_client_address(entry)
        if address is None:
            return None
        normalized.append(address)

    proxy_networks = trusted_networks + cloudflare_networks
    while normalized and address_in_networks(normalized[-1], proxy_networks):
        normalized.pop()

    if not normalized:
        return None
    return normalized[-1]


def _parse_forwarded_header(header_value: str) -> str | None:
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        raw = match.group(1) or match.group(2) or ""
        address = normalize_client_address(raw)
        if address is None:
            return None
        return address
    return None


def _immediate_peer(request: Request) -> tuple[str | None, bool]:
    """Return normalized peer address and whether normalization used the raw host."""
    if request.client is None:
        return None, False
    raw = request.client.host.strip()
    if not raw:
        return None, False
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized, False
    return raw, True


def _trusted_forwarding_path(
    peer: str,
    peer_is_raw_host: bool,
    xff_chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """True when the request arrived through a configured trusted proxy hop."""
    if peer_is_raw_host:
        return False
    if address_in_networks(peer, trusted_networks):
        return True
    if not xff_chain:
        return False
    leftmost = normalize_client_address(xff_chain[0])
    rightmost = normalize_client_address(xff_chain[-1])
    if leftmost is None or rightmost is None:
        return False
    if peer != leftmost:
        return False
    return address_in_networks(rightmost, trusted_networks)


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _emit_source_resolution_telemetry(path: SourceResolutionPath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_resolution_path": path.value},
    )


def _maybe_emit_invalid_forwarding_telemetry(path: SourceResolutionPath) -> None:
    global _last_invalid_forwarded_log_at, _invalid_forwarded_log_count

    if path not in (
        SourceResolutionPath.UNTRUSTED_HEADERS_REJECTED,
        SourceResolutionPath.MALFORMED_FORWARDING,
    ):
        return

    now = time.monotonic()
    if now - _last_invalid_forwarded_log_at < _INVALID_FORWARDED_SAMPLE_INTERVAL_SECONDS:
        if _invalid_forwarded_log_count >= _INVALID_FORWARDED_SAMPLE_CAP:
            return
        _invalid_forwarded_log_count += 1
    else:
        _last_invalid_forwarded_log_at = now
        _invalid_forwarded_log_count = 1

    _logger.info(
        "Admin login forwarding headers rejected",
        extra={"source_resolution_path": path.value},
    )


def reset_source_resolution_telemetry() -> None:
    """Reset sampled invalid-forwarding counters (tests only)."""
    global _last_invalid_forwarded_log_at, _invalid_forwarded_log_count
    _last_invalid_forwarded_log_at = 0.0
    _invalid_forwarded_log_count = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Production chain (saberistic.com): browser → Cloudflare edge → Render proxy →
    Uvicorn. Forwarding headers are parsed only when the immediate TCP peer is in
    ``ADMIN_TRUSTED_PROXY_CIDRS``. The rightmost untrusted ``X-Forwarded-For`` hop
    is used. ``CF-Connecting-IP`` is accepted only when a configured Cloudflare
    proxy range appears in the forwarded chain (direct Render origin requests
    cannot spoof it). Raw addresses and header chains are never logged.
    """
    if request.client is None:
        _maybe_emit_invalid_forwarding_telemetry(SourceResolutionPath.MISSING_PEER)
        _emit_source_resolution_telemetry(SourceResolutionPath.MISSING_PEER)
        return ClientSourceResolution("unknown", SourceResolutionPath.MISSING_PEER)

    peer, peer_is_raw_host = _immediate_peer(request)
    if peer is None:
        _maybe_emit_invalid_forwarding_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        _emit_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        return ClientSourceResolution("unknown", SourceResolutionPath.MALFORMED_FORWARDING)

    trusted_networks = settings.admin_trusted_proxy_networks
    cloudflare_networks = settings.admin_cloudflare_proxy_networks
    proxy_trust_enabled = settings.admin_trust_proxy_headers and bool(trusted_networks)

    if not proxy_trust_enabled:
        if _has_forwarding_headers(request):
            _maybe_emit_invalid_forwarding_telemetry(
                SourceResolutionPath.UNTRUSTED_HEADERS_REJECTED
            )
        _emit_source_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarded_chain(xff_header) if xff_header else []
    if xff_chain is None:
        _maybe_emit_invalid_forwarding_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        _emit_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        return ClientSourceResolution(peer, SourceResolutionPath.MALFORMED_FORWARDING)

    if not _trusted_forwarding_path(peer, peer_is_raw_host, xff_chain, trusted_networks):
        if _has_forwarding_headers(request):
            _maybe_emit_invalid_forwarding_telemetry(
                SourceResolutionPath.UNTRUSTED_HEADERS_REJECTED
            )
        _emit_source_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    xff_client = (
        _resolve_from_forwarded_chain(xff_chain, trusted_networks, cloudflare_networks)
        if xff_chain
        else None
    )

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_client = normalize_client_address(cf_header) if cf_header else None
    if cf_header and cf_client is None:
        _maybe_emit_invalid_forwarding_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        _emit_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
        return ClientSourceResolution(peer, SourceResolutionPath.MALFORMED_FORWARDING)

    chain_for_cf_proof = list(xff_chain)
    if peer not in chain_for_cf_proof:
        chain_for_cf_proof.append(peer)
    cloudflare_hop_seen = bool(cloudflare_networks) and any(
        address_in_networks(entry, cloudflare_networks) for entry in chain_for_cf_proof
    )

    if cf_client is not None and cloudflare_hop_seen:
        if xff_client is None or cf_client == xff_client:
            _emit_source_resolution_telemetry(SourceResolutionPath.CF_CONNECTING_IP)
            return ClientSourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)

    if xff_client is not None:
        _emit_source_resolution_telemetry(SourceResolutionPath.TRUSTED_XFF_CHAIN)
        return ClientSourceResolution(xff_client, SourceResolutionPath.TRUSTED_XFF_CHAIN)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_client = _parse_forwarded_header(forwarded_header)
        if forwarded_header and forwarded_client is None:
            _maybe_emit_invalid_forwarding_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
            _emit_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING)
            return ClientSourceResolution(peer, SourceResolutionPath.MALFORMED_FORWARDING)
        if forwarded_client is not None:
            _emit_source_resolution_telemetry(SourceResolutionPath.FORWARDED_HEADER)
            return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED_HEADER)

    _emit_source_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
    return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)
