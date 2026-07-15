"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARD_CHAIN_LENGTH = 32
_TELEMETRY_SAMPLE_RATE = 100
_telemetry_counter = 0

# Default trusted hops: Render internal proxy / loopback / RFC1918.
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

# Subset of Cloudflare published IPv4 ranges for hop proof (full list in render.yaml).
_DEFAULT_CLOUDFLARE_TRUST_CIDRS = (
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

_FORWARDED_PAIR_RE = re.compile(
    r'for=(?:"\[?([^";]+)\]?"|([^";]+))',
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source was resolved."""

    DIRECT_PEER = "direct_peer"
    CF_CONNECTING_IP = "cf_connecting_ip"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED_RFC = "forwarded_rfc"
    PEER_FALLBACK = "peer_fallback"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without raw forwarding metadata."""

    address: str
    path: SourceResolutionPath


def parse_network_list(raw: str, *, defaults: Sequence[str] = ()) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated IPs/CIDRs into networks; ignore invalid tokens."""
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens:
        tokens = list(defaults)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in tokens:
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                addr = ipaddress.ip_address(token)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; strip ports when unambiguous."""
    value = raw.strip()
    if not value:
        return None

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
        remainder = value[end + 1 :]
        if remainder.startswith(":") and remainder[1:].isdigit():
            value = host
        else:
            return None
    elif value.count(":") == 1 and "." in value:
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host

    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def _ip_in_networks(address: str, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _peer_address(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    if not host:
        return None
    return normalize_client_address(host) or host


def _parse_x_forwarded_for(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [part.strip() for part in header_value.split(",")]


def _parse_forwarded_header(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    addresses: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_PAIR_RE.search(entry)
        if match is None:
            continue
        candidate = match.group(1) or match.group(2) or ""
        addresses.append(candidate.strip())
    return addresses


def _normalized_chain(raw_chain: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for raw in raw_chain:
        address = normalize_client_address(raw)
        if address is not None:
            normalized.append(address)
    return normalized


def _client_from_trusted_chain(
    raw_chain: Sequence[str],
    *,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    if not raw_chain:
        return None
    if len(raw_chain) > MAX_FORWARD_CHAIN_LENGTH:
        return None

    chain = _normalized_chain(raw_chain)
    if not chain:
        return None

    if len(chain) == 1:
        return chain[0]

    skip_networks = tuple(trusted_networks) + tuple(cloudflare_networks)
    index = len(chain) - 1
    while index >= 0 and _ip_in_networks(chain[index], skip_networks):
        index -= 1
    if index < 0:
        return None
    return chain[index]


def _cloudflare_hop_proven(
    raw_chain: Sequence[str],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    if not cloudflare_networks:
        return False
    for raw in raw_chain:
        normalized = normalize_client_address(raw)
        if normalized and _ip_in_networks(normalized, cloudflare_networks):
            return True
    return False


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _emit_source_telemetry(path: SourceResolutionPath, *, invalid_forwarding: bool = False) -> None:
    global _telemetry_counter
    _telemetry_counter += 1
    if not invalid_forwarding and _telemetry_counter % _TELEMETRY_SAMPLE_RATE != 0:
        return
    _logger.info(
        "Admin login client source resolved",
        extra={
            "resolution_path": path.value,
            "invalid_forwarding": invalid_forwarding,
        },
    )


def reset_source_resolution_telemetry() -> None:
    """Reset telemetry sampling counter (tests only)."""
    global _telemetry_counter
    _telemetry_counter = 0


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_network_list(
        settings.admin_trusted_proxy_cidrs,
        defaults=_DEFAULT_TRUSTED_PROXY_CIDRS,
    )


def cloudflare_trust_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not settings.admin_cloudflare_trust_cidrs.strip():
        return ()
    return parse_network_list(
        settings.admin_cloudflare_trust_cidrs,
        defaults=_DEFAULT_CLOUDFLARE_TRUST_CIDRS,
    )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective admin-login limiter source with verified proxy trust."""
    peer = _peer_address(request)
    peer_address = peer or "unknown"
    trusted_networks = trusted_proxy_networks(settings)
    cloudflare_networks = cloudflare_trust_networks(settings)

    peer_trusted = bool(
        peer
        and settings.admin_trust_proxy_headers
        and _ip_in_networks(peer, trusted_networks)
    )

    if not peer_trusted:
        if _has_forwarding_headers(request):
            _emit_source_telemetry(SourceResolutionPath.DIRECT_PEER, invalid_forwarding=True)
        return ClientSourceResolution(peer_address, SourceResolutionPath.DIRECT_PEER)

    xff_chain = _parse_x_forwarded_for(request.headers.get("x-forwarded-for"))

    cf_header = request.headers.get("cf-connecting-ip")
    if cf_header:
        cf_address = normalize_client_address(cf_header)
        if cf_address and _cloudflare_hop_proven(xff_chain, cloudflare_networks):
            _emit_source_telemetry(SourceResolutionPath.CF_CONNECTING_IP)
            return ClientSourceResolution(cf_address, SourceResolutionPath.CF_CONNECTING_IP)
        _emit_source_telemetry(SourceResolutionPath.PEER_FALLBACK, invalid_forwarding=True)

    xff_client = _client_from_trusted_chain(
        xff_chain,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    )
    if xff_client:
        _emit_source_telemetry(SourceResolutionPath.X_FORWARDED_FOR)
        return ClientSourceResolution(xff_client, SourceResolutionPath.X_FORWARDED_FOR)

    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded"))
    forwarded_client = _client_from_trusted_chain(
        forwarded_chain,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    )
    if forwarded_client:
        _emit_source_telemetry(SourceResolutionPath.FORWARDED_RFC)
        return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED_RFC)

    if _has_forwarding_headers(request):
        _emit_source_telemetry(SourceResolutionPath.PEER_FALLBACK, invalid_forwarding=True)
    else:
        _emit_source_telemetry(SourceResolutionPath.PEER_FALLBACK)

    if peer:
        return ClientSourceResolution(peer, SourceResolutionPath.PEER_FALLBACK)
    return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN)
