"""Verified-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_REJECTION_LOG_INTERVAL_SECONDS = 60.0

_IPV4_WITH_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d+$")
_IPV6_BRACKET_PORT_RE = re.compile(r"^\[([^\]]+)\]:\d+$")
_FORWARDED_FOR_PARAM_RE = re.compile(
    r"for=(?P<value>(?:\"[^\"]+\")|[^;,\s]+)",
    re.IGNORECASE,
)

_rejection_log_state: dict[str, float] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and bounded telemetry metadata."""

    source: str
    path: str
    header_family: str | None = None


def reset_proxy_trust_telemetry() -> None:
    """Clear rate-limited forwarding telemetry state (tests only)."""
    _rejection_log_state.clear()


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted proxy CIDRs and host addresses."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            elif ":" in candidate:
                networks.append(ipaddress.ip_network(f"{candidate}/128", strict=False))
            else:
                networks.append(ipaddress.ip_network(f"{candidate}/32", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically (ports and mapped forms)."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith('"') and candidate.endswith('"'):
        candidate = candidate[1:-1].strip()
    bracket_match = _IPV6_BRACKET_PORT_RE.match(candidate)
    if bracket_match:
        candidate = bracket_match.group(1)
    elif _IPV4_WITH_PORT_RE.match(candidate):
        candidate = candidate.rsplit(":", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return address.compressed.lower()
    return str(address)


def address_in_trusted_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    if not host:
        return None
    return host.strip() or None


def _split_forwarding_chain(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",")]


def _walk_x_forwarded_for_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    normalized_hops: list[str] = []
    for hop in reversed(chain):
        if not hop:
            continue
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)
    if not normalized_hops:
        return None
    if not address_in_trusted_networks(normalized_hops[0], trusted_networks):
        return None
    for hop in normalized_hops[1:]:
        if not address_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _parse_forwarded_header(
    raw: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not entries or len(entries) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    hop_addresses: list[str] = []
    for entry in reversed(entries):
        match = _FORWARDED_FOR_PARAM_RE.search(entry)
        if match is None:
            return None
        normalized = normalize_ip_address(match.group("value"))
        if normalized is None:
            return None
        hop_addresses.append(normalized)
    if not hop_addresses:
        return None
    if not address_in_trusted_networks(hop_addresses[0], trusted_networks):
        return None
    for hop in hop_addresses[1:]:
        if not address_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _log_forwarding_rejection(path: str, reason: str) -> None:
    now = time.monotonic()
    key = f"{path}:{reason}"
    last_logged = _rejection_log_state.get(key, 0.0)
    if now - last_logged < _REJECTION_LOG_INTERVAL_SECONDS:
        return
    _rejection_log_state[key] = now
    _logger.info(
        "Admin login client source forwarding rejected",
        extra={
            "source_resolution_path": path,
            "forwarding_rejection_reason": reason,
        },
    )


def _resolution_from_peer(peer: str | None, *, path: str) -> ClientSourceResolution:
    if peer is None:
        return ClientSourceResolution(source="unknown", path=path)
    normalized_peer = normalize_ip_address(peer)
    if normalized_peer is None:
        return ClientSourceResolution(source="unknown", path=path)
    return ClientSourceResolution(source=normalized_peer, path=path)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source through verified proxy hops.

    Production chain: public client → Cloudflare edge → Render load balancer →
    Uvicorn worker. Forwarded identity is accepted only when the immediate TCP
    peer is a configured trusted proxy. Header precedence for trusted peers:

    1. ``CF-Connecting-IP`` (single validated address)
    2. ``X-Forwarded-For`` (right-to-left trusted-hop walk)
    3. ``Forwarded`` (RFC 7239 ``for=`` right-to-left trusted-hop walk)
    4. Immediate peer address
    """
    peer_host = _immediate_peer_host(request)
    peer_normalized = normalize_ip_address(peer_host) if peer_host else None

    if not settings.admin_trust_proxy_headers:
        return _resolution_from_peer(peer_normalized, path="direct_peer")

    trusted_networks = settings.admin_trusted_proxy_networks
    if not trusted_networks:
        _log_forwarding_rejection("untrusted_peer", "missing_trusted_proxy_cidrs")
        return _resolution_from_peer(peer_normalized, path="untrusted_peer")

    if peer_normalized is None or not address_in_trusted_networks(
        peer_normalized, trusted_networks
    ):
        _log_forwarding_rejection("untrusted_peer", "peer_not_trusted")
        return _resolution_from_peer(peer_normalized, path="untrusted_peer")

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        normalized_cf = normalize_ip_address(cf_connecting_ip)
        if normalized_cf is None:
            _log_forwarding_rejection("invalid_forwarding", "cf_connecting_ip_invalid")
        else:
            return ClientSourceResolution(
                source=normalized_cf,
                path="cf_connecting_ip",
                header_family="cf_connecting_ip",
            )

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        chain = _split_forwarding_chain(x_forwarded_for)
        resolved = _walk_x_forwarded_for_chain(chain, trusted_networks=trusted_networks)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path="x_forwarded_for",
                header_family="x_forwarded_for",
            )
        _log_forwarding_rejection("invalid_forwarding", "x_forwarded_for_untrusted")

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        resolved = _parse_forwarded_header(forwarded, trusted_networks=trusted_networks)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path="forwarded",
                header_family="forwarded",
            )
        _log_forwarding_rejection("invalid_forwarding", "forwarded_untrusted")

    return _resolution_from_peer(peer_normalized, path="trusted_peer_fallback")


def proxy_trust_health_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification metadata for /health."""
    return {
        "resolution_model": "verified_hop",
        "proxy_headers_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_cidr_count": len(settings.admin_trusted_proxy_networks),
        "uvicorn_proxy_headers_enabled": False,
    }
