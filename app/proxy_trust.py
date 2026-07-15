"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Production Render private-network boundary (also referenced in render.yaml).
RENDER_TRUSTED_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32"

MAX_FORWARDING_CHAIN_LENGTH = 10

_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_invalid_forwarding_log_lock = Lock()
_last_invalid_forwarding_log_monotonic = 0.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    TRUSTED_FORWARDED = "trusted_forwarded"
    UNKNOWN_PEER = "unknown_peer"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: SourceResolutionPath
    rejected_forwarding: bool = False


def parse_trusted_proxy_cidrs(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted-proxy CIDRs; ignore malformed entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def is_trusted_proxy_address(host: str, trusted_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    """Return whether ``host`` falls inside the configured trusted-proxy boundary."""
    if not trusted_cidrs:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in trusted_cidrs)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; strip ports and IPv4-mapped forms."""
    value = raw.strip()
    if not value:
        return None

    host = value
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
    elif value.count(":") == 1 and "." in value:
        host, _port = value.rsplit(":", 1)
        if not _port.isdigit():
            return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def _split_forwarding_chain(raw: str) -> list[str] | None:
    parts = [segment.strip() for segment in raw.split(",")]
    elements = [segment for segment in parts if segment]
    if not elements:
        return None
    if len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return elements


def _client_from_x_forwarded_for(
    raw_header: str,
    *,
    trusted_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk X-Forwarded-For right-to-left, skipping trusted proxy hops."""
    chain = _split_forwarding_chain(raw_header)
    if chain is None:
        return None

    for hop in reversed(chain):
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        if not is_trusted_proxy_address(normalized, trusted_cidrs):
            return normalized

    leftmost = normalize_ip_address(chain[0])
    if leftmost is None:
        return None
    if is_trusted_proxy_address(leftmost, trusted_cidrs):
        return None
    return leftmost


_FOR_PARAM_RE = re.compile(r"for=(?:" r"\"([^\"]+)\"|" r"\[([^\]]+)\]|" r"([^;,\s]+)" r")", re.IGNORECASE)


def _client_from_forwarded_header(
    raw_header: str,
    *,
    trusted_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Parse RFC 7239 Forwarded elements right-to-left."""
    elements = [segment.strip() for segment in raw_header.split(",") if segment.strip()]
    if not elements or len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for element in reversed(elements):
        match = _FOR_PARAM_RE.search(element)
        if match is None:
            return None
        candidate = match.group(1) or match.group(2) or match.group(3)
        if candidate is None:
            return None
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return None
        if not is_trusted_proxy_address(normalized, trusted_cidrs):
            return normalized

    first = _FOR_PARAM_RE.search(elements[0])
    if first is None:
        return None
    candidate = first.group(1) or first.group(2) or first.group(3)
    if candidate is None:
        return None
    normalized = normalize_ip_address(candidate)
    if normalized is None or is_trusted_proxy_address(normalized, trusted_cidrs):
        return None
    return normalized


def _maybe_log_rejected_forwarding(path: SourceResolutionPath) -> None:
    global _last_invalid_forwarding_log_monotonic
    now = time.monotonic()
    with _invalid_forwarding_log_lock:
        if now - _last_invalid_forwarding_log_monotonic < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _last_invalid_forwarding_log_monotonic = now
    _logger.info(
        "Admin login source resolution rejected forwarding data",
        extra={"source_resolution_path": path.value},
    )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResult:
    """Resolve the effective admin-login limiter source for ``request``.

    Production chain: browser → Cloudflare edge → Render load balancer → Uvicorn.

    Forwarding headers are honored only when the immediate TCP peer is inside the
    configured trusted-proxy boundary. Header precedence (trusted peer only):

    1. ``CF-Connecting-IP`` — Cloudflare client address
    2. ``X-Forwarded-For`` — right-to-left trusted-hop walk
    3. ``Forwarded`` — RFC 7239 ``for=`` right-to-left walk

    Direct peers and untrusted forwarding data fall back to the normalized peer
    address, or ``unknown`` when the peer cannot be parsed.
    """
    if request.client is None:
        return ClientSourceResult("unknown", SourceResolutionPath.UNKNOWN_PEER)

    normalized_peer = normalize_ip_address(request.client.host)
    if normalized_peer is None:
        return ClientSourceResult("unknown", SourceResolutionPath.UNKNOWN_PEER)

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResult(normalized_peer, SourceResolutionPath.DIRECT_PEER)

    trusted_cidrs = settings.admin_trusted_proxy_cidrs
    if not is_trusted_proxy_address(normalized_peer, trusted_cidrs):
        rejected = bool(
            request.headers.get("x-forwarded-for")
            or request.headers.get("forwarded")
            or request.headers.get("cf-connecting-ip")
        )
        if rejected:
            _maybe_log_rejected_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResult(
            normalized_peer,
            SourceResolutionPath.DIRECT_PEER,
            rejected_forwarding=rejected,
        )

    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    if cf_header:
        cf_client = normalize_ip_address(cf_header)
        if cf_client is not None:
            return ClientSourceResult(
                cf_client,
                SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
            )
        _maybe_log_rejected_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResult(
            normalized_peer,
            SourceResolutionPath.INVALID_FORWARDING,
            rejected_forwarding=True,
        )

    xff_header = request.headers.get("x-forwarded-for", "").strip()
    if xff_header:
        xff_client = _client_from_x_forwarded_for(xff_header, trusted_cidrs=trusted_cidrs)
        if xff_client is not None:
            return ClientSourceResult(xff_client, SourceResolutionPath.TRUSTED_XFF)
        _maybe_log_rejected_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResult(
            normalized_peer,
            SourceResolutionPath.INVALID_FORWARDING,
            rejected_forwarding=True,
        )

    forwarded_header = request.headers.get("forwarded", "").strip()
    if forwarded_header:
        forwarded_client = _client_from_forwarded_header(
            forwarded_header,
            trusted_cidrs=trusted_cidrs,
        )
        if forwarded_client is not None:
            return ClientSourceResult(forwarded_client, SourceResolutionPath.TRUSTED_FORWARDED)
        _maybe_log_rejected_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResult(
            normalized_peer,
            SourceResolutionPath.INVALID_FORWARDING,
            rejected_forwarding=True,
        )

    return ClientSourceResult(normalized_peer, SourceResolutionPath.DIRECT_PEER)
