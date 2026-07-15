"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import Iterable, Union

from fastapi import Request

from app.config import Settings

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

_logger = logging.getLogger(__name__)

# Conservative cap on comma-separated forwarding hops (Cloudflare + Render + margin).
_MAX_FORWARD_HOPS = 20

# Sample one invalid/untrusted forwarding attempt per N observations.
_INVALID_TELEMETRY_SAMPLE_EVERY = 100

_INVALID_TELEMETRY_LOCK = threading.Lock()
_invalid_telemetry_counter = 0

# RFC 7239 Forwarded: for=203.0.113.1 or for="[2001:db8::1]"
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^"]+)\]"|"?([^";,\s]+)"?)',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without retaining raw forwarding headers."""

    source: str
    path: str


def parse_trusted_networks(raw: str) -> tuple[Network, ...]:
    """Parse comma-separated IP addresses and CIDR blocks."""
    networks: list[Network] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
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


def _host_in_trusted_networks(
    host: str, networks: Iterable[Network]
) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 strings; strip ports and IPv4-mapped IPv6."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        # IPv4 with port (203.0.113.1:8080)
        candidate = candidate.rsplit(":", 1)[0]
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return addr.compressed


def _split_forwarded_for(raw: str) -> list[str]:
    if not raw or len(raw) > 4096:
        return []
    parts = [segment.strip() for segment in raw.split(",") if segment.strip()]
    if len(parts) > _MAX_FORWARD_HOPS:
        return []
    return parts


def _parse_forwarded_header(raw: str) -> list[str]:
    if not raw or len(raw) > 4096:
        return []
    values: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        host = match.group(1) or match.group(2) or ""
        host = host.strip()
        if host:
            values.append(host)
    if len(values) > _MAX_FORWARD_HOPS:
        return []
    return values


def _walk_forward_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[Network, ...],
) -> str | None:
    """Select the rightmost non-trusted hop (original client behind proxies)."""
    normalized: list[str] = []
    for hop in chain:
        normalized_hop = normalize_client_address(hop)
        if normalized_hop is None:
            return None
        normalized.append(normalized_hop)

    for hop in reversed(normalized):
        if not _host_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    raw = request.client.host.strip()
    if not raw:
        return None
    normalized = normalize_client_address(raw)
    return normalized if normalized is not None else raw


def _cloudflare_hop_present(
    chain: list[str],
    *,
    cloudflare_networks: tuple[Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized and _host_in_trusted_networks(normalized, cloudflare_networks):
            return True
    return False


def _maybe_log_invalid_resolution(path: str, reason: str) -> None:
    global _invalid_telemetry_counter
    with _INVALID_TELEMETRY_LOCK:
        _invalid_telemetry_counter += 1
        sample = _invalid_telemetry_counter
    if sample % _INVALID_TELEMETRY_SAMPLE_EVERY != 1:
        return
    _logger.info(
        "Admin login source resolution rejected forwarding data",
        extra={"source_resolution_path": path, "reject_reason": reason},
    )


def reset_proxy_trust_telemetry() -> None:
    """Clear sampled invalid-resolution counter (tests only)."""
    global _invalid_telemetry_counter
    with _INVALID_TELEMETRY_LOCK:
        _invalid_telemetry_counter = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Trust model (production: Cloudflare → Render load balancer → Uvicorn):

    1. When the immediate TCP peer is **not** in ``ADMIN_TRUSTED_PROXY_IPS``,
       use the peer address and ignore all forwarding headers.
    2. When the immediate peer **is** trusted, walk ``X-Forwarded-For`` right to
       left, skipping hops in the trusted boundary, and select the first
       non-trusted address.
    3. If ``X-Forwarded-For`` is missing or unusable, apply the same walk to
       RFC 7239 ``Forwarded`` ``for=`` values.
    4. ``CF-Connecting-IP`` is honored only when ``ADMIN_TRUST_CLOUDFLARE_EDGE``
       is enabled **and** a Cloudflare hop appears in the forwarding chain;
       it must agree with the chain-derived client or is ignored.
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    skip_networks = trusted_networks + settings.admin_cloudflare_networks
    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path="missing_peer")

    if not trusted_networks or not _host_in_trusted_networks(peer, trusted_networks):
        if _forwarding_headers_present(request):
            _maybe_log_invalid_resolution("direct_peer", "untrusted_peer_with_forwarding")
        return ClientSourceResolution(source=peer, path="direct_peer")

    xff_chain = _split_forwarded_for(request.headers.get("x-forwarded-for", ""))
    client = _walk_forward_chain(xff_chain, trusted_networks=skip_networks)
    path = "forwarded_chain"

    if client is None and xff_chain:
        _maybe_log_invalid_resolution("malformed_fallback", "invalid_x_forwarded_for")
        return ClientSourceResolution(source="unknown", path="malformed_fallback")

    if client is None:
        forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))
        client = _walk_forward_chain(forwarded_chain, trusted_networks=skip_networks)
        path = "forwarded_rfc7239"
        if client is None and forwarded_chain:
            _maybe_log_invalid_resolution("malformed_fallback", "invalid_forwarded")
            return ClientSourceResolution(source="unknown", path="malformed_fallback")

    if client is None:
        _maybe_log_invalid_resolution("malformed_fallback", "empty_forwarding_chain")
        return ClientSourceResolution(source="unknown", path="malformed_fallback")

    if settings.admin_trust_cloudflare_edge:
        cf_raw = request.headers.get("cf-connecting-ip", "")
        cf_client = normalize_client_address(cf_raw) if cf_raw else None
        cf_hop_seen = _cloudflare_hop_present(
            xff_chain,
            cloudflare_networks=settings.admin_cloudflare_networks,
        )
        if cf_client and cf_hop_seen:
            if cf_client == client:
                path = "cf_connecting_ip"
            else:
                _maybe_log_invalid_resolution(
                    "forwarded_chain",
                    "cf_connecting_ip_conflict",
                )
        elif cf_raw and not cf_hop_seen:
            _maybe_log_invalid_resolution(
                "forwarded_chain",
                "cf_connecting_ip_without_edge_hop",
            )

    return ClientSourceResolution(source=client, path=path)


def _forwarding_headers_present(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def log_source_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    """Emit bounded structured telemetry without raw addresses or headers."""
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path},
    )
