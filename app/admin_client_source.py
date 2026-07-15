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

_FORWARDED_FOR_MAX_LENGTH = 2048
_FORWARDED_HEADER_MAX_LENGTH = 4096
_MAX_CHAIN_HOPS = 32
_UNKNOWN_SOURCE = "unknown"

# Bounded telemetry (no raw addresses or header values).
_TELEMETRY_LOCK = threading.Lock()
_TELEMETRY_COUNTS: dict[str, int] = {}
_TELEMETRY_LAST_EMIT = 0.0
_TELEMETRY_EMIT_INTERVAL_SECONDS = 60.0
_TELEMETRY_MAX_PATHS = 32

# Resolution paths surfaced in structured logs (stable identifiers only).
PATH_DIRECT_PEER = "direct_peer"
PATH_FORWARDED_CHAIN = "forwarded_chain"
PATH_FORWARDED_RFC7239 = "forwarded_rfc7239"
PATH_CF_CONNECTING_IP = "cf_connecting_ip"
PATH_UNTRUSTED_FORWARDED = "untrusted_forwarded"
PATH_INVALID_FORWARDED = "invalid_forwarded"
PATH_MISSING_PEER = "missing_peer"

_FORWARDED_PAIR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"\s*([^";]+)\s*"|([^";,\s]+))',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


def reset_client_source_telemetry() -> None:
    """Clear telemetry counters (tests only)."""
    with _TELEMETRY_LOCK:
        _TELEMETRY_COUNTS.clear()


def client_source_telemetry_snapshot() -> dict[str, int]:
    """Return a copy of resolution-path counters (tests only)."""
    with _TELEMETRY_LOCK:
        return dict(_TELEMETRY_COUNTS)


def _networks_from_cidrs(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            parsed.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            _logger.warning(
                "Ignoring invalid admin trusted-proxy CIDR",
                extra={"cidr_length": len(cidr)},
            )
    return tuple(parsed)


def _normalize_ip(raw: str) -> str | None:
    """Return a deterministic normalized IP string or ``None`` when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return address.compressed
    return str(address)


def _address_in_networks(ip: str, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    normalized = _normalize_ip(ip)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    raw = request.client.host.strip()
    if not raw:
        return None
    normalized = _normalize_ip(raw)
    if normalized is not None:
        return normalized
    return raw.lower()


def _split_forwarded_for(header_value: str) -> list[str]:
    if len(header_value) > _FORWARDED_FOR_MAX_LENGTH:
        return []
    elements = [part.strip() for part in header_value.split(",")]
    if len(elements) > _MAX_CHAIN_HOPS:
        return []
    if any(not element for element in elements):
        return []
    return elements


def _trusted_hop_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    hop_cidrs = settings.admin_trusted_forward_hop_cidrs
    proxy_cidrs = settings.admin_trusted_proxy_cidrs
    edge_cidrs = settings.admin_trusted_edge_cidrs
    combined = tuple(dict.fromkeys((*hop_cidrs, *proxy_cidrs, *edge_cidrs)))
    return _networks_from_cidrs(combined)


def _trusted_peer_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _networks_from_cidrs(settings.admin_trusted_proxy_cidrs)


def _trusted_edge_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _networks_from_cidrs(settings.admin_trusted_edge_cidrs)


def _client_from_forwarded_chain(
    chain: list[str],
    *,
    trusted_hops: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain:
        return None
    normalized_chain: list[str] = []
    for hop in chain:
        normalized = _normalize_ip(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)
    for hop in reversed(normalized_chain):
        if _address_in_networks(hop, trusted_hops):
            continue
        return hop
    return None


def _parse_forwarded_header(header_value: str) -> list[str]:
    if len(header_value) > _FORWARDED_HEADER_MAX_LENGTH:
        return []
    addresses: list[str] = []
    for segment in header_value.split(","):
        match = _FORWARDED_PAIR_RE.search(segment)
        if match is None:
            continue
        raw = match.group(1) or match.group(2) or match.group(3) or ""
        normalized = _normalize_ip(raw)
        if normalized is None:
            return []
        addresses.append(normalized)
        if len(addresses) > _MAX_CHAIN_HOPS:
            return []
    return addresses


def _chain_contains_edge(
    chain: list[str],
    *,
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(_address_in_networks(hop, edge_networks) for hop in chain)


def _record_telemetry(path: str, *, sample_invalid: bool = False) -> None:
    with _TELEMETRY_LOCK:
        if len(_TELEMETRY_COUNTS) >= _TELEMETRY_MAX_PATHS and path not in _TELEMETRY_COUNTS:
            path = "other"
        _TELEMETRY_COUNTS[path] = _TELEMETRY_COUNTS.get(path, 0) + 1
        should_emit = sample_invalid or path in {
            PATH_UNTRUSTED_FORWARDED,
            PATH_INVALID_FORWARDED,
        }
        global _TELEMETRY_LAST_EMIT
        now = time.monotonic()
        if should_emit and now - _TELEMETRY_LAST_EMIT >= _TELEMETRY_EMIT_INTERVAL_SECONDS:
            _TELEMETRY_LAST_EMIT = now
            _logger.info(
                "Admin login client-source resolution telemetry",
                extra={
                    "resolution_paths": dict(_TELEMETRY_COUNTS),
                },
            )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain: ``Client → Cloudflare → Render load balancer → Uvicorn``.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_CIDRS``. The left-most ``X-Forwarded-For`` value is
    never trusted directly; the parser walks the chain from the immediate peer
    side and selects the first hop that is not a configured trusted proxy, edge,
    or forward-hop network.
    """
    peer = _immediate_peer(request)
    if peer is None:
        _record_telemetry(PATH_MISSING_PEER)
        return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=PATH_MISSING_PEER)

    if not settings.admin_trust_proxy_headers:
        _record_telemetry(PATH_DIRECT_PEER)
        return ClientSourceResolution(source=peer, path=PATH_DIRECT_PEER)

    trusted_peer_networks = _trusted_peer_networks(settings)
    if not _address_in_networks(peer, trusted_peer_networks):
        if _has_suspicious_forwarding_headers(request):
            _record_telemetry(PATH_UNTRUSTED_FORWARDED, sample_invalid=True)
        _record_telemetry(PATH_DIRECT_PEER)
        return ClientSourceResolution(source=peer, path=PATH_DIRECT_PEER)

    trusted_hops = _trusted_hop_networks(settings)
    edge_networks = _trusted_edge_networks(settings)

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarded_for(xff_raw) if xff_raw else []
    if xff_raw and not xff_chain:
        _record_telemetry(PATH_INVALID_FORWARDED, sample_invalid=True)
        return ClientSourceResolution(source=peer, path=PATH_INVALID_FORWARDED)

    if xff_chain:
        client = _client_from_forwarded_chain(xff_chain, trusted_hops=trusted_hops)
        if client is not None:
            _record_telemetry(PATH_FORWARDED_CHAIN)
            return ClientSourceResolution(source=client, path=PATH_FORWARDED_CHAIN)

        cf_raw = request.headers.get("cf-connecting-ip", "")
        if cf_raw:
            cf_client = _normalize_ip(cf_raw)
            if cf_client is not None and _chain_contains_edge(
                xff_chain, edge_networks=edge_networks
            ):
                _record_telemetry(PATH_CF_CONNECTING_IP)
                return ClientSourceResolution(source=cf_client, path=PATH_CF_CONNECTING_IP)

        _record_telemetry(PATH_INVALID_FORWARDED, sample_invalid=True)
        return ClientSourceResolution(source=peer, path=PATH_INVALID_FORWARDED)

    forwarded_raw = request.headers.get("forwarded", "")
    forwarded_chain = _parse_forwarded_header(forwarded_raw) if forwarded_raw else []
    if forwarded_raw and not forwarded_chain:
        _record_telemetry(PATH_INVALID_FORWARDED, sample_invalid=True)
        return ClientSourceResolution(source=peer, path=PATH_INVALID_FORWARDED)
    if forwarded_chain:
        client = _client_from_forwarded_chain(forwarded_chain, trusted_hops=trusted_hops)
        if client is None:
            _record_telemetry(PATH_INVALID_FORWARDED, sample_invalid=True)
            return ClientSourceResolution(source=peer, path=PATH_INVALID_FORWARDED)
        _record_telemetry(PATH_FORWARDED_RFC7239)
        return ClientSourceResolution(source=client, path=PATH_FORWARDED_RFC7239)

    cf_raw = request.headers.get("cf-connecting-ip", "")
    if cf_raw:
        cf_client = _normalize_ip(cf_raw)
        if cf_client is None:
            _record_telemetry(PATH_INVALID_FORWARDED, sample_invalid=True)
            return ClientSourceResolution(source=peer, path=PATH_INVALID_FORWARDED)
        # Vendor header alone is never sufficient; require a trusted edge hop in XFF/Forwarded.
        combined_chain = [*xff_chain, *forwarded_chain]
        if _chain_contains_edge(combined_chain, edge_networks=edge_networks):
            _record_telemetry(PATH_CF_CONNECTING_IP)
            return ClientSourceResolution(source=cf_client, path=PATH_CF_CONNECTING_IP)
        _record_telemetry(PATH_UNTRUSTED_FORWARDED, sample_invalid=True)

    _record_telemetry(PATH_DIRECT_PEER)
    return ClientSourceResolution(source=peer, path=PATH_DIRECT_PEER)


def _has_suspicious_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).source


def deployment_trust_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive trust-boundary summary for health/deploy verification."""
    return {
        "trust_enabled": settings.admin_trust_proxy_headers,
        "uvicorn_proxy_headers": settings.uvicorn_proxy_headers,
        "uvicorn_forwarded_allow_ips": settings.uvicorn_forwarded_allow_ips,
        "trusted_proxy_cidr_count": len(settings.admin_trusted_proxy_cidrs),
        "trusted_forward_hop_cidr_count": len(settings.admin_trusted_forward_hop_cidrs),
        "trusted_edge_cidr_count": len(settings.admin_trusted_edge_cidrs),
    }
