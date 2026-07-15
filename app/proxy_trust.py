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

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_FORWARDED_HEADER_BYTES = 2048
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0
_last_invalid_telemetry_at = 0.0

# Cloudflare published edge ranges (IPv4 + IPv6) used only to recognize
# infrastructure hops in X-Forwarded-For chains — not as peer-trust proof.
_CLOUDFLARE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("173.245.48.0/20"),
    ipaddress.ip_network("103.21.244.0/22"),
    ipaddress.ip_network("103.22.200.0/22"),
    ipaddress.ip_network("103.31.4.0/22"),
    ipaddress.ip_network("141.101.64.0/18"),
    ipaddress.ip_network("108.162.192.0/18"),
    ipaddress.ip_network("190.93.240.0/20"),
    ipaddress.ip_network("188.114.96.0/20"),
    ipaddress.ip_network("197.234.240.0/22"),
    ipaddress.ip_network("198.41.128.0/17"),
    ipaddress.ip_network("162.158.0.0/15"),
    ipaddress.ip_network("104.16.0.0/13"),
    ipaddress.ip_network("104.24.0.0/14"),
    ipaddress.ip_network("172.64.0.0/13"),
    ipaddress.ip_network("131.0.72.0/22"),
    ipaddress.ip_network("2400:cb00::/32"),
    ipaddress.ip_network("2606:4700::/32"),
    ipaddress.ip_network("2803:f800::/32"),
    ipaddress.ip_network("2405:b500::/32"),
    ipaddress.ip_network("2405:8100::/32"),
    ipaddress.ip_network("2a06:98c0::/29"),
    ipaddress.ip_network("2c0f:f248::/32"),
)

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?:(?:\"([^\"]+)\")|(?:\[([^\]]+)\])|([^;,\"]+))",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    INVALID_FORWARDED = "invalid_forwarded"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: SourceResolutionPath



def normalize_client_address(raw_value: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. IPv4-mapped) or return None when invalid."""
    candidate = raw_value.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    zone_split = candidate.split("%", 1)
    host = zone_split[0]
    if host.count(":") == 1 and "." in host:
        host = host.rsplit(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    for network in networks:
        if address in network:
            return True
    return False


def _parse_ip_address(raw_value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    normalized = normalize_client_address(raw_value)
    if normalized is None:
        return None
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        return None


def immediate_peer_host(request: Request) -> str | None:
    """Return the TCP peer host without consulting forwarding headers."""
    if request.client is None:
        return None
    return request.client.host or None


def _chain_strip_networks(
    peer_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return peer_networks + _CLOUDFLARE_NETWORKS


def _header_values(request: Request, name: str) -> list[str]:
    target = name.lower()
    values: list[str] = []
    raw_headers = getattr(request.headers, "raw", None)
    if raw_headers is not None:
        for key, value in raw_headers:
            if key.decode("latin-1").lower() == target:
                values.append(value.decode("latin-1"))
        return values
    for key, value in request.headers.items():
        if key.lower() == target:
            values.append(value)
    return values


def _collect_x_forwarded_for(request: Request) -> list[str] | None:
    values = _header_values(request, "x-forwarded-for")
    if not values:
        return []
    combined = ",".join(values)
    if len(combined.encode("utf-8")) > MAX_FORWARDED_HEADER_BYTES:
        return None
    parts = combined.split(",")
    if len(parts) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return parts


def _resolve_from_xff_chain(
    chain_parts: list[str],
    *,
    peer_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain_parts:
        return None

    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for part in chain_parts:
        address = _parse_ip_address(part)
        if address is None:
            return None
        parsed.append(address)

    strip_networks = _chain_strip_networks(peer_networks)
    remaining = list(parsed)
    while len(remaining) > 1 and _address_in_networks(remaining[-1], strip_networks):
        remaining.pop()

    if not remaining:
        return None
    return normalize_client_address(str(remaining[-1]))


def _cloudflare_hop_present(
    chain_parts: list[str],
) -> bool:
    for part in chain_parts:
        address = _parse_ip_address(part)
        if address is None:
            continue
        if _address_in_networks(address, _CLOUDFLARE_NETWORKS):
            return True
    return False


def _parse_forwarded_header(request: Request) -> str | None:
    values = _header_values(request, "forwarded")
    if not values:
        return None
    if len(",".join(values).encode("utf-8")) > MAX_FORWARDED_HEADER_BYTES:
        return None
    for entry in reversed(values):
        for token in entry.split(";"):
            token = token.strip()
            match = _FORWARDED_FOR_TOKEN.match(token)
            if not match:
                continue
            raw = match.group(1) or match.group(2) or match.group(3)
            if raw is None:
                continue
            normalized = normalize_client_address(raw)
            if normalized is not None:
                return normalized
    return None


def _emit_invalid_forwarded_telemetry() -> None:
    global _last_invalid_telemetry_at
    now = time.monotonic()
    if now - _last_invalid_telemetry_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
        return
    _last_invalid_telemetry_at = now
    _logger.info(
        "Admin login source resolution rejected forwarding headers",
        extra={"source_resolution_path": SourceResolutionPath.INVALID_FORWARDED.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    *,
    peer_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_IPS``. The left-most raw ``X-Forwarded-For`` value
    is never trusted directly; infrastructure hops (configured peers and
    Cloudflare edge ranges) are stripped from the right before selecting the
    client. ``CF-Connecting-IP`` is accepted only when a Cloudflare hop appears
    in the validated ``X-Forwarded-For`` chain.
    """
    peer_raw = immediate_peer_host(request)
    if peer_raw is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.MISSING_PEER)

    peer_normalized = normalize_client_address(peer_raw)
    if peer_normalized is None:
        peer_normalized = peer_raw.strip() or "unknown"

    peer_address = _parse_ip_address(peer_normalized)
    if peer_address is None:
        return ClientSourceResolution(peer_normalized, SourceResolutionPath.UNTRUSTED_PEER)

    if not peer_networks or not _address_in_networks(peer_address, peer_networks):
        return ClientSourceResolution(peer_normalized, SourceResolutionPath.UNTRUSTED_PEER)

    xff_parts = _collect_x_forwarded_for(request)
    if xff_parts is None:
        _emit_invalid_forwarded_telemetry()
        return ClientSourceResolution(peer_normalized, SourceResolutionPath.INVALID_FORWARDED)

    xff_client = _resolve_from_xff_chain(xff_parts, peer_networks=peer_networks)
    if xff_parts and xff_client is None:
        _emit_invalid_forwarded_telemetry()
        return ClientSourceResolution(peer_normalized, SourceResolutionPath.INVALID_FORWARDED)

    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    cf_client: str | None = None
    if cf_header and xff_parts and _cloudflare_hop_present(xff_parts):
        cf_client = normalize_client_address(cf_header)

    if xff_client is not None:
        if cf_client is not None and cf_client != xff_client:
            return ClientSourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)
        return ClientSourceResolution(xff_client, SourceResolutionPath.XFF_TRUSTED_CHAIN)

    if cf_client is not None:
        return ClientSourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)

    forwarded_client = _parse_forwarded_header(request)
    if forwarded_client is not None:
        return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED_HEADER)

    return ClientSourceResolution(peer_normalized, SourceResolutionPath.DIRECT_PEER)


def record_source_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    """Emit bounded structured telemetry without raw addresses or header chains."""
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path.value},
    )
