"""Verified proxy-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 10
MAX_FORWARDING_HEADER_LENGTH = 1024
_INVALID_FORWARDING_SAMPLE_RATE = 100
_invalid_forwarding_counter = 0

# Render load balancers and local loopback; overridable via env.
DEFAULT_TRUSTED_PROXY_NETWORKS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)

# Published Cloudflare IPv4 ranges (https://www.cloudflare.com/ips-v4).
# Override with ADMIN_CLOUDFLARE_EDGE_NETWORKS when Cloudflare updates ranges.
DEFAULT_CLOUDFLARE_EDGE_NETWORKS = (
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


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    CLOUDFLARE_CONNECTING_IP = "cloudflare_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    MISSING_SOURCE = "missing_source"
    MALFORMED_FORWARDING = "malformed_forwarding"
    AMBIGUOUS_FORWARDING = "ambiguous_forwarding"
    UNTRUSTED_FORWARDING = "untrusted_forwarding"


@dataclass(frozen=True)
class ResolvedClientSource:
    """Normalized client source material for limiter key derivation."""

    address: str
    path: SourceResolutionPath


def _parse_networks(spec: str, *, fallback: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = spec.strip()
    if not raw:
        return tuple(ipaddress.ip_network(value, strict=False) for value in fallback)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    if not networks:
        return tuple(ipaddress.ip_network(value, strict=False) for value in fallback)
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse_networks(
        settings.admin_trusted_proxy_networks,
        fallback=DEFAULT_TRUSTED_PROXY_NETWORKS,
    )


def cloudflare_edge_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse_networks(
        settings.admin_cloudflare_edge_networks,
        fallback=DEFAULT_CLOUDFLARE_EDGE_NETWORKS,
    )


def _ip_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address in network for network in networks)


def _strip_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if value.count(":") == 1 and "." in value:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            return host
    return value


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 deterministically; return None when invalid."""
    if raw is None:
        return None
    candidate = _strip_port(raw.strip())
    if not candidate:
        return None
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() or None


def _parse_forwarding_chain(header_value: str) -> list[str] | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    parts = [segment.strip() for segment in header_value.split(",")]
    if not parts or any(not segment for segment in parts):
        return None
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return parts


def _chain_hosts(
    chain: list[str],
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    hosts: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        hosts.append(ipaddress.ip_address(normalized))
    return hosts


def _trusted_skip_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return trusted_proxy_networks(settings) + cloudflare_edge_networks(settings)


def _chain_contains_cloudflare_edge(
    hosts: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    settings: Settings,
) -> bool:
    edge_networks = cloudflare_edge_networks(settings)
    return any(_ip_in_networks(host, edge_networks) for host in hosts)


def _client_from_forwarded_chain(
    chain: list[str],
    settings: Settings,
) -> str | None:
    hosts = _chain_hosts(chain)
    if hosts is None:
        return None
    skip_networks = _trusted_skip_networks(settings)
    for host in reversed(hosts):
        if not _ip_in_networks(host, skip_networks):
            return normalize_client_address(str(host))
    if hosts:
        return normalize_client_address(str(hosts[0]))
    return None


def _peer_source_address(peer_raw: str | None) -> str:
    if peer_raw is None:
        return "unknown"
    normalized = normalize_client_address(peer_raw)
    if normalized is not None:
        return normalized
    stripped = peer_raw.strip()
    return stripped.lower() if stripped else "unknown"


def _extract_forwarded_for_values(header_value: str) -> list[str]:
    values: list[str] = []
    for entry in header_value.split(","):
        segment = entry.strip()
        if not segment:
            continue
        for part in segment.split(";"):
            token = part.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip()
            if not value:
                continue
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            elif value.startswith("[") and "]" in value:
                end = value.index("]")
                value = value[1:end]
            if value.casefold() == "unknown":
                continue
            values.append(value)
    return values


def _client_from_forwarded_header(header_value: str, settings: Settings) -> str | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    chain = _extract_forwarded_for_values(header_value)
    if not chain:
        return None
    return _client_from_forwarded_chain(chain, settings)


def _record_invalid_forwarding(path: SourceResolutionPath) -> None:
    global _invalid_forwarding_counter
    _invalid_forwarding_counter += 1
    if _invalid_forwarding_counter % _INVALID_FORWARDING_SAMPLE_RATE != 0:
        return
    _logger.info(
        "Admin login source forwarding rejected",
        extra={"source_resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ResolvedClientSource:
    """Resolve limiter source identity with verified proxy-hop trust.

    Production chain: public client → Cloudflare edge → Render load balancer →
    Uvicorn. Forwarding headers are parsed only when the immediate TCP peer is a
    member of ``ADMIN_TRUSTED_PROXY_NETWORKS``. Vendor headers such as
    ``CF-Connecting-IP`` are accepted only after a Cloudflare edge hop appears in
    the verified forwarding chain.
    """
    peer_raw = _immediate_peer_host(request)
    peer_normalized = normalize_client_address(peer_raw)
    peer_source = _peer_source_address(peer_raw)

    if not settings.admin_trust_proxy_headers:
        if peer_raw is None:
            return ResolvedClientSource("unknown", SourceResolutionPath.MISSING_SOURCE)
        return ResolvedClientSource(peer_source, SourceResolutionPath.DIRECT_PEER)

    proxy_networks = trusted_proxy_networks(settings)
    if peer_raw is None:
        return ResolvedClientSource("unknown", SourceResolutionPath.MISSING_SOURCE)

    if peer_normalized is None:
        _record_invalid_forwarding(SourceResolutionPath.UNTRUSTED_PEER)
        return ResolvedClientSource(peer_source, SourceResolutionPath.UNTRUSTED_PEER)

    peer_addr = ipaddress.ip_address(peer_normalized)
    if not _ip_in_networks(peer_addr, proxy_networks):
        _record_invalid_forwarding(SourceResolutionPath.UNTRUSTED_PEER)
        return ResolvedClientSource(peer_source, SourceResolutionPath.UNTRUSTED_PEER)

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _parse_forwarding_chain(xff_raw) if xff_raw else None
    if xff_raw and xff_chain is None:
        _record_invalid_forwarding(SourceResolutionPath.MALFORMED_FORWARDING)
        return ResolvedClientSource("unknown", SourceResolutionPath.MALFORMED_FORWARDING)

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    cf_normalized = normalize_client_address(cf_connecting_ip) if cf_connecting_ip else None
    if cf_connecting_ip and cf_normalized is None:
        _record_invalid_forwarding(SourceResolutionPath.MALFORMED_FORWARDING)
        return ResolvedClientSource("unknown", SourceResolutionPath.MALFORMED_FORWARDING)

    edge_verified = False
    if xff_chain is not None:
        hosts = _chain_hosts(xff_chain)
        if hosts is None:
            _record_invalid_forwarding(SourceResolutionPath.MALFORMED_FORWARDING)
            return ResolvedClientSource("unknown", SourceResolutionPath.MALFORMED_FORWARDING)
        edge_verified = _chain_contains_cloudflare_edge(hosts, settings)

    if cf_normalized and edge_verified:
        return ResolvedClientSource(
            cf_normalized,
            SourceResolutionPath.CLOUDFLARE_CONNECTING_IP,
        )

    if cf_normalized and not edge_verified:
        _record_invalid_forwarding(SourceResolutionPath.UNTRUSTED_FORWARDING)

    if xff_chain is not None:
        client = _client_from_forwarded_chain(xff_chain, settings)
        if client is None:
            _record_invalid_forwarding(SourceResolutionPath.MALFORMED_FORWARDING)
            return ResolvedClientSource("unknown", SourceResolutionPath.MALFORMED_FORWARDING)
        return ResolvedClientSource(client, SourceResolutionPath.FORWARDED_CHAIN)

    forwarded_raw = request.headers.get("forwarded", "")
    if forwarded_raw:
        client = _client_from_forwarded_header(forwarded_raw, settings)
        if client is None:
            _record_invalid_forwarding(SourceResolutionPath.MALFORMED_FORWARDING)
            return ResolvedClientSource("unknown", SourceResolutionPath.MALFORMED_FORWARDING)
        return ResolvedClientSource(client, SourceResolutionPath.FORWARDED_HEADER)

    return ResolvedClientSource(peer_source, SourceResolutionPath.DIRECT_PEER)


def client_ip(request: Request, settings: Settings) -> str:
    """Return normalized client source material for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).address
