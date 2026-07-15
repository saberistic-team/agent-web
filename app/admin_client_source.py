"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0

_invalid_forwarding_lock = threading.Lock()
_invalid_forwarding_last_logged_at = 0.0
_invalid_forwarding_suppressed = 0

# RFC 7239 Forwarded: for=...;proto=... (comma-separated entries).
_FORWARDED_FOR_TOKEN = re.compile(
    r"for=(?P<value>(?:\"[^\"]+\")|(?:\[[^\]]+\](?::\d+)?)|(?:[^;,\s]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a privacy-safe telemetry path label."""

    source: str
    path: str


def normalize_ip_address(raw: str) -> str | None:
    """Return a deterministic canonical IP string or ``None`` when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate[1:].partition("]")
        candidate = host
        if remainder.startswith(":"):
            port = remainder[1:]
            if not port.isdigit():
                return None

    if candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    if parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return parsed.compressed


def _ip_in_networks(
    ip_literal: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        parsed = ipaddress.ip_address(ip_literal)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _trusted_hop_networks(settings: Settings) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    return settings.admin_trusted_proxy_cidrs + settings.admin_cloudflare_proxy_cidrs


def _source_from_peer(peer: str | None) -> str:
    if not peer:
        return "unknown"
    normalized = normalize_ip_address(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip()
    return stripped if stripped else "unknown"


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() if host else None


def _parse_x_forwarded_for_chain(raw_header: str) -> tuple[str, ...] | None:
    if not raw_header.strip():
        return None
    elements = [element.strip() for element in raw_header.split(",")]
    if not elements or any(not element for element in elements):
        return None
    if len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    normalized: list[str] = []
    for element in elements:
        parsed = normalize_ip_address(element)
        if parsed is None:
            return None
        normalized.append(parsed)
    return tuple(normalized)


def _parse_forwarded_for_chain(raw_header: str) -> tuple[str, ...] | None:
    if not raw_header.strip():
        return None
    values: list[str] = []
    for match in _FORWARDED_FOR_TOKEN.finditer(raw_header):
        token = match.group("value").strip().strip('"')
        parsed = normalize_ip_address(token)
        if parsed is None:
            return None
        values.append(parsed)
    if not values or len(values) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return tuple(values)


def _resolve_from_forwarding_chain(
    chain: tuple[str, ...],
    *,
    peer_ip: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str:
    trusted_hops = {peer_ip}
    for hop in chain:
        if _ip_in_networks(hop, trusted_networks):
            trusted_hops.add(hop)

    for hop in reversed(chain):
        if hop not in trusted_hops:
            return hop
    return chain[0]


def _cloudflare_hop_present(
    chain: tuple[str, ...] | None,
    settings: Settings,
) -> bool:
    if not chain or not settings.admin_cloudflare_proxy_cidrs:
        return False
    return any(
        _ip_in_networks(hop, settings.admin_cloudflare_proxy_cidrs) for hop in chain
    )


def _vendor_client_header(
    request: Request,
    settings: Settings,
    *,
    xff_chain: tuple[str, ...] | None,
) -> str | None:
    if not _cloudflare_hop_present(xff_chain, settings):
        return None

    for header_name in ("cf-connecting-ip", "true-client-ip"):
        candidate = normalize_ip_address(request.headers.get(header_name, ""))
        if candidate is not None:
            return candidate
    return None


def _record_invalid_forwarding(reason: str) -> None:
    global _invalid_forwarding_last_logged_at, _invalid_forwarding_suppressed

    now = time.monotonic()
    with _invalid_forwarding_lock:
        elapsed = now - _invalid_forwarding_last_logged_at
        if elapsed < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            _invalid_forwarding_suppressed += 1
            return
        suppressed = _invalid_forwarding_suppressed
        _invalid_forwarding_suppressed = 0
        _invalid_forwarding_last_logged_at = now

    _logger.info(
        "Admin login source forwarding rejected",
        extra={
            "source_resolution_reason": reason,
            "suppressed_since_last_log": suppressed,
        },
    )


def reset_invalid_forwarding_telemetry() -> None:
    """Clear rate-limited invalid-forwarding counters (tests only)."""
    global _invalid_forwarding_last_logged_at, _invalid_forwarding_suppressed
    with _invalid_forwarding_lock:
        _invalid_forwarding_last_logged_at = 0.0
        _invalid_forwarding_suppressed = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login source address for shared limiter buckets.

    Production chain: public client → Cloudflare edge → Render load balancer → Uvicorn.

    Forwarding headers are honored only when the immediate ASGI peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. The leftmost ``X-Forwarded-For`` value is never
    trusted blindly; trusted hops are stripped from the right and the nearest
    remaining hop is used. Vendor headers such as ``CF-Connecting-IP`` are accepted
    only when a Cloudflare hop is present in the validated forwarding chain.
    """
    peer = _peer_host(request)
    peer_normalized = normalize_ip_address(peer) if peer else None

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(
            source=_source_from_peer(peer),
            path="direct_peer",
        )

    peer_normalized = normalize_ip_address(peer) if peer else None

    if peer_normalized is None:
        _record_invalid_forwarding("missing_peer")
        return ClientSourceResolution(source=_source_from_peer(peer), path="unknown_peer")

    if not _ip_in_networks(peer_normalized, settings.admin_trusted_proxy_cidrs):
        _record_invalid_forwarding("untrusted_peer")
        return ClientSourceResolution(source=peer_normalized, path="direct_peer")

    trusted_networks = _trusted_hop_networks(settings)
    xff_chain = _parse_x_forwarded_for_chain(request.headers.get("x-forwarded-for", ""))
    if xff_chain is not None:
        resolved = _resolve_from_forwarding_chain(
            xff_chain,
            peer_ip=peer_normalized,
            trusted_networks=trusted_networks,
        )
        if _ip_in_networks(resolved, settings.admin_cloudflare_proxy_cidrs):
            vendor_ip = _vendor_client_header(request, settings, xff_chain=xff_chain)
            if vendor_ip is not None:
                return ClientSourceResolution(source=vendor_ip, path="cf_connecting_ip")
        return ClientSourceResolution(source=resolved, path="forwarded_chain")

    forwarded_chain = _parse_forwarded_for_chain(request.headers.get("forwarded", ""))
    if forwarded_chain is not None:
        resolved = _resolve_from_forwarding_chain(
            forwarded_chain,
            peer_ip=peer_normalized,
            trusted_networks=trusted_networks,
        )
        return ClientSourceResolution(source=resolved, path="forwarded_rfc7239")

    vendor_ip = _vendor_client_header(
        request,
        settings,
        xff_chain=_parse_x_forwarded_for_chain(request.headers.get("x-forwarded-for", "")),
    )
    if vendor_ip is not None:
        return ClientSourceResolution(source=vendor_ip, path="cf_connecting_ip")

    _record_invalid_forwarding("missing_forwarding_chain")
    return ClientSourceResolution(source=peer_normalized, path="peer_fallback")


def log_admin_login_source_resolution(resolution: ClientSourceResolution) -> None:
    """Emit bounded telemetry without raw addresses or forwarding headers."""
    _logger.info(
        "Admin login source resolved",
        extra={"source_resolution_path": resolution.path},
    )
