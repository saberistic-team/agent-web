"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

UNKNOWN_SOURCE = "unknown"
MAX_FORWARD_CHAIN_LENGTH = 10
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0

# Cloudflare published IPv4 ranges (https://www.cloudflare.com/ips-v4/).
CLOUDFLARE_EDGE_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)

# Cloudflare published IPv6 ranges (https://www.cloudflare.com/ips-v6/).
CLOUDFLARE_EDGE_CIDRS_V6: tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_FORWARDED_FOR_TOKEN = re.compile(
    r"for=(?P<value>(?:\"[^\"]+\")|\[[^\]]+\]|[^;,\s]+)",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels for admin login source resolution."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    XFF_TRUSTED_WALK = "xff_trusted_walk"
    FORWARDED_TRUSTED_WALK = "forwarded_trusted_walk"
    CF_CONNECTING_IP = "cf_connecting_ip"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class SourceResolution:
    """Resolved limiter source and the path used to derive it."""

    source: str
    path: SourceResolutionPath


@dataclass(frozen=True)
class _ParsedNetworks:
    proxy: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    edge: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


_telemetry_lock = threading.Lock()
_telemetry_counts: dict[str, int] = {}
_telemetry_last_emit = 0.0
_parsed_networks_cache: dict[tuple[str, ...], _ParsedNetworks] = {}


def reset_source_resolution_telemetry() -> None:
    """Clear in-memory telemetry counters (tests only)."""
    global _telemetry_last_emit
    with _telemetry_lock:
        _telemetry_counts.clear()
        _telemetry_last_emit = 0.0


def source_resolution_telemetry_snapshot() -> dict[str, int]:
    """Return a copy of sampled source-resolution path counters."""
    with _telemetry_lock:
        return dict(_telemetry_counts)


def _record_source_resolution_telemetry(path: SourceResolutionPath) -> None:
    global _telemetry_last_emit
    now = time.monotonic()
    with _telemetry_lock:
        _telemetry_counts[path.value] = _telemetry_counts.get(path.value, 0) + 1
        should_emit = now - _telemetry_last_emit >= _TELEMETRY_SAMPLE_INTERVAL_SECONDS
        if should_emit:
            _telemetry_last_emit = now
            snapshot = dict(_telemetry_counts)
    if should_emit:
        _logger.info(
            "Admin login source resolution telemetry",
            extra={"admin_source_resolution_counts": snapshot},
        )


def _parse_network_list(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        candidate = cidr.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _networks_for_settings(settings: Settings) -> _ParsedNetworks:
    proxy_cidrs = settings.admin_trusted_proxy_cidrs
    edge_cidrs = list(settings.admin_trusted_edge_cidrs)
    if settings.admin_trust_cloudflare_edge:
        edge_cidrs.extend(CLOUDFLARE_EDGE_CIDRS)
        edge_cidrs.extend(CLOUDFLARE_EDGE_CIDRS_V6)
    cache_key = (proxy_cidrs, tuple(edge_cidrs))
    cached = _parsed_networks_cache.get(cache_key)
    if cached is not None:
        return cached
    parsed = _ParsedNetworks(
        proxy=_parse_network_list(proxy_cidrs),
        edge=_parse_network_list(tuple(edge_cidrs)),
    )
    _parsed_networks_cache[cache_key] = parsed
    return parsed


def normalize_ip_address(value: str) -> str | None:
    """Normalize IPv4/IPv6 addresses for deterministic limiter keys."""
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.lower() == UNKNOWN_SOURCE:
        return UNKNOWN_SOURCE

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate[1:].partition("]")
        if remainder.startswith(":"):
            candidate = host
        else:
            candidate = host

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.rpartition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _ip_in_networks(
    ip_value: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(ip_value)
    if normalized is None or normalized == UNKNOWN_SOURCE:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    if not header_value.strip():
        return []
    parts = [part.strip() for part in header_value.split(",")]
    return [part for part in parts if part]


def parse_x_forwarded_for(header_value: str) -> list[str]:
    """Parse comma-separated X-Forwarded-For values without selecting a client."""
    return _split_forwarding_chain(header_value)


def parse_forwarded_header(header_value: str) -> list[str]:
    """Parse RFC 7239 Forwarded header ``for=`` values in left-to-right order."""
    if not header_value.strip():
        return []
    values: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            continue
        raw = match.group("value").strip().strip('"')
        if raw.lower().startswith("unknown"):
            continue
        values.append(raw)
    return values


def _trusted_walk_client(
    hops: list[str],
    *,
    proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not hops:
        return None
    if len(hops) > MAX_FORWARD_CHAIN_LENGTH:
        return None

    trusted_networks = proxy_networks + edge_networks
    for hop in reversed(hops):
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        if _ip_in_networks(normalized, trusted_networks):
            continue
        return normalized
    return None


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    return normalize_ip_address(request.client.host)


def _edge_present_in_chain(
    hops: list[str],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not edge_networks:
        return False
    return any(_ip_in_networks(hop, edge_networks) for hop in hops)


def _resolve_from_forwarding_headers(
    request: Request,
    *,
    proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    immediate_peer: str,
) -> SourceResolution | None:
    xff_hops = parse_x_forwarded_for(request.headers.get("x-forwarded-for", ""))
    forwarded_hops = parse_forwarded_header(request.headers.get("forwarded", ""))

    if xff_hops and forwarded_hops:
        xff_client = _trusted_walk_client(
            [*xff_hops, immediate_peer],
            proxy_networks=proxy_networks,
            edge_networks=edge_networks,
        )
        forwarded_client = _trusted_walk_client(
            [*forwarded_hops, immediate_peer],
            proxy_networks=proxy_networks,
            edge_networks=edge_networks,
        )
        if xff_client is None or forwarded_client is None:
            return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.INVALID_FORWARDING)
        if xff_client != forwarded_client:
            return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.INVALID_FORWARDING)

    hops: list[str]
    path: SourceResolutionPath
    if xff_hops:
        hops = xff_hops
        path = SourceResolutionPath.XFF_TRUSTED_WALK
    elif forwarded_hops:
        hops = forwarded_hops
        path = SourceResolutionPath.FORWARDED_TRUSTED_WALK
    else:
        hops = []
        path = SourceResolutionPath.INVALID_FORWARDING

    if not hops:
        cf_header = request.headers.get("cf-connecting-ip", "")
        cf_client = normalize_ip_address(cf_header)
        if cf_client and _edge_present_in_chain([immediate_peer], edge_networks):
            return SourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)
        return None

    client = _trusted_walk_client(
        [*hops, immediate_peer],
        proxy_networks=proxy_networks,
        edge_networks=edge_networks,
    )
    if client is None:
        return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.INVALID_FORWARDING)

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_client = normalize_ip_address(cf_header)
    if cf_client and _edge_present_in_chain([*hops, immediate_peer], edge_networks):
        if cf_client != client:
            return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.INVALID_FORWARDING)
        return SourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)

    return SourceResolution(client, path)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Resolve the effective admin-login client source for rate limiting."""
    resolution = resolve_admin_login_client_source_detail(request, settings)
    _record_source_resolution_telemetry(resolution.path)
    return resolution.source


def resolve_admin_login_client_source_detail(
    request: Request,
    settings: Settings,
) -> SourceResolution:
    """Resolve client source and return the resolution path for tests/telemetry."""
    peer = _immediate_peer(request)
    if peer is None:
        return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.MISSING_PEER)

    if not settings.admin_trust_proxy_headers:
        return SourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    networks = _networks_for_settings(settings)
    if not networks.proxy:
        return SourceResolution(peer, SourceResolutionPath.UNTRUSTED_PEER)

    if not _ip_in_networks(peer, networks.proxy):
        return SourceResolution(peer, SourceResolutionPath.UNTRUSTED_PEER)

    forwarded = _resolve_from_forwarding_headers(
        request,
        proxy_networks=networks.proxy,
        edge_networks=networks.edge,
        immediate_peer=peer,
    )
    if forwarded is not None:
        return forwarded

    return SourceResolution(UNKNOWN_SOURCE, SourceResolutionPath.INVALID_FORWARDING)


def admin_proxy_trust_health(settings: Settings) -> dict[str, Any]:
    """Non-sensitive deployment verification payload for /health."""
    proxy_configured = bool(settings.admin_trusted_proxy_cidrs)
    edge_configured = bool(
        settings.admin_trusted_edge_cidrs or settings.admin_trust_cloudflare_edge
    )
    return {
        "proxy_headers_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_configured": proxy_configured,
        "trusted_edge_configured": edge_configured,
        "forwarded_allow_ips": settings.uvicorn_forwarded_allow_ips,
        "resolution_model": "trusted_hop_walk",
    }
