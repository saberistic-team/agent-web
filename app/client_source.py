"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

UNKNOWN_SOURCE = "unknown"
_MAX_FORWARDED_CHAIN_LENGTH = 32
_FORWARDED_FOR_HEADER = "x-forwarded-for"
_FORWARDED_HEADER = "forwarded"
_CF_CONNECTING_IP_HEADER = "cf-connecting-ip"
_FORWARDED_FOR_PARAM = re.compile(
    r'^\s*for=(?:"(?P<quoted>[^"]+)"|(?P<unquoted>[^;,\s]+))',
    re.IGNORECASE,
)
_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_telemetry_counts: dict[str, int] = {}
_untrusted_forwarding_last_logged = 0.0
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0


class ClientSourceResolutionPath(str, Enum):
    """Bounded telemetry labels for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    FORWARDED_RFC7239 = "forwarded_rfc7239"
    CF_CONNECTING_IP_CONFIRMED = "cf_connecting_ip_confirmed"
    MALFORMED_FORWARDING = "malformed_forwarding"
    MISSING_PEER = "missing_peer"
    UNTRUSTED_FORWARDING_IGNORED = "untrusted_forwarding_ignored"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: ClientSourceResolutionPath


def parse_trusted_proxy_cidrs(raw: str) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Parse comma-separated trusted proxy CIDR literals."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def production_trusted_proxy_cidrs() -> str:
    """Default trusted-proxy boundary for Render private networking."""
    return "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"


def resolve_trusted_proxy_cidrs(settings: Settings) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Return configured trusted proxy networks, including legacy env fallback."""
    if settings.admin_trusted_proxy_cidrs:
        return parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    if settings.admin_trust_proxy_headers:
        return parse_trusted_proxy_cidrs(production_trusted_proxy_cidrs())
    return ()


def normalize_client_source(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 source material deterministically."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    host = candidate
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    elif host.count(":") == 1 and "." in host:
        host = host.rsplit(":", 1)[0]

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        lowered = candidate.lower()
        return lowered if lowered else None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def is_trusted_proxy_address(
    address: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return whether ``address`` belongs to a configured trusted proxy network."""
    normalized = normalize_client_source(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    if host is None:
        return None
    stripped = host.strip()
    return stripped or None


def _parse_forwarded_for_chain(raw_header: str) -> list[str]:
    elements = [item.strip() for item in raw_header.split(",")]
    if len(elements) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    if any(not element for element in elements):
        return []
    return elements


def _non_trusted_hops(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> list[str]:
    hops: list[str] = []
    for hop in chain:
        normalized = normalize_client_source(hop)
        if normalized is None:
            return []
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            return []
        if any(parsed in network for network in trusted_networks):
            continue
        hops.append(normalized)
    return hops


def _resolve_right_to_left(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    *,
    cf_connecting_ip: str | None = None,
) -> str | None:
    non_trusted = _non_trusted_hops(chain, trusted_networks)
    if not non_trusted:
        return None
    candidate = non_trusted[-1]
    if len(non_trusted) == 1:
        return candidate
    if cf_connecting_ip and cf_connecting_ip == candidate:
        return candidate
    return None


def _parse_rfc_forwarded_for(raw_header: str) -> list[str]:
    values: list[str] = []
    for entry in raw_header.split(","):
        match = _FORWARDED_FOR_PARAM.match(entry)
        if match is None:
            continue
        candidate = match.group("quoted") or match.group("unquoted")
        if candidate:
            values.append(candidate.strip())
    if len(values) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    if any(not value for value in values):
        return []
    return values


def _header_value(request: Request, header_name: str) -> str | None:
    value = request.headers.get(header_name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_from_forwarding_headers(
    request: Request,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, ClientSourceResolutionPath]:
    """Resolve client source from forwarding headers using trusted-hop parsing."""
    xff_raw = _header_value(request, _FORWARDED_FOR_HEADER)
    xff_chain = _parse_forwarded_for_chain(xff_raw) if xff_raw else []
    if xff_raw and not xff_chain:
        return None, ClientSourceResolutionPath.MALFORMED_FORWARDING

    if xff_chain:
        cf_raw = _header_value(request, _CF_CONNECTING_IP_HEADER)
        cf_normalized = normalize_client_source(cf_raw) if cf_raw else None
        resolved = _resolve_right_to_left(
            xff_chain,
            trusted_networks,
            cf_connecting_ip=cf_normalized,
        )
        if resolved is None:
            return None, ClientSourceResolutionPath.MALFORMED_FORWARDING
        if cf_normalized and cf_normalized == resolved:
            return resolved, ClientSourceResolutionPath.CF_CONNECTING_IP_CONFIRMED
        return resolved, ClientSourceResolutionPath.FORWARDED_CHAIN

    forwarded_raw = _header_value(request, _FORWARDED_HEADER)
    forwarded_chain = _parse_rfc_forwarded_for(forwarded_raw) if forwarded_raw else []
    if forwarded_raw and not forwarded_chain:
        return None, ClientSourceResolutionPath.MALFORMED_FORWARDING
    if forwarded_chain:
        cf_raw = _header_value(request, _CF_CONNECTING_IP_HEADER)
        cf_normalized = normalize_client_source(cf_raw) if cf_raw else None
        resolved = _resolve_right_to_left(
            forwarded_chain,
            trusted_networks,
            cf_connecting_ip=cf_normalized,
        )
        if resolved is None:
            return None, ClientSourceResolutionPath.MALFORMED_FORWARDING
        path = ClientSourceResolutionPath.FORWARDED_RFC7239
        if cf_normalized and cf_normalized == resolved:
            return resolved, ClientSourceResolutionPath.CF_CONNECTING_IP_CONFIRMED
        return resolved, path

    cf_raw = _header_value(request, _CF_CONNECTING_IP_HEADER)
    if cf_raw:
        return None, ClientSourceResolutionPath.MALFORMED_FORWARDING

    return None, ClientSourceResolutionPath.MALFORMED_FORWARDING


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        _header_value(request, header_name) is not None
        for header_name in (
            _FORWARDED_FOR_HEADER,
            _FORWARDED_HEADER,
            _CF_CONNECTING_IP_HEADER,
        )
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for shared limiter buckets.

    Production chain: public client → Cloudflare → Render load balancer → Uvicorn.

    When Uvicorn runs with ``--proxy-headers`` and a restricted
    ``--forwarded-allow-ips`` boundary, ``request.client`` is already the
    right-to-left resolved client for trusted immediate peers. When the peer is
    still inside the trusted boundary (for example in tests or misconfiguration),
    this helper parses forwarding headers itself and never trusts a left-most
    ``X-Forwarded-For`` value from an unverified hop.
    """
    trusted_networks = resolve_trusted_proxy_cidrs(settings)
    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution(UNKNOWN_SOURCE, ClientSourceResolutionPath.MISSING_PEER)

    if not trusted_networks:
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding_attempt()
        normalized_peer = normalize_client_source(peer)
        if normalized_peer is None:
            return ClientSourceResolution(peer.lower(), ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)

    normalized_peer = normalize_client_source(peer)
    if normalized_peer is None:
        return ClientSourceResolution(peer.lower(), ClientSourceResolutionPath.DIRECT_PEER)

    if not is_trusted_proxy_address(normalized_peer, trusted_networks):
        return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)

    resolved, path = _resolve_from_forwarding_headers(request, trusted_networks)
    if resolved is None:
        return ClientSourceResolution(UNKNOWN_SOURCE, path)
    return ClientSourceResolution(resolved, path)


def record_client_source_telemetry(resolution: ClientSourceResolution) -> None:
    """Record bounded, privacy-preserving source-resolution telemetry."""
    with _telemetry_lock:
        _telemetry_counts[resolution.path.value] = (
            _telemetry_counts.get(resolution.path.value, 0) + 1
        )


def reset_client_source_telemetry() -> None:
    """Clear in-memory telemetry counters (tests only)."""
    global _untrusted_forwarding_last_logged
    with _telemetry_lock:
        _telemetry_counts.clear()
    _untrusted_forwarding_last_logged = 0.0


def client_source_telemetry_snapshot() -> dict[str, int]:
    """Return a copy of in-memory resolution-path counters (tests/ops)."""
    with _telemetry_lock:
        return dict(_telemetry_counts)


def _record_untrusted_forwarding_attempt() -> None:
    global _untrusted_forwarding_last_logged
    record_client_source_telemetry(
        ClientSourceResolution(
            UNKNOWN_SOURCE,
            ClientSourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED,
        )
    )
    now = time.monotonic()
    if now - _untrusted_forwarding_last_logged < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _untrusted_forwarding_last_logged = now
    _logger.info(
        "Ignored untrusted admin login forwarding headers",
        extra={"resolution_path": ClientSourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED.value},
    )
