"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Render private-network proxies that terminate TLS before the app process.
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_INVALID_TELEMETRY_WINDOW_SECONDS = 60
_INVALID_TELEMETRY_MAX_SAMPLES = 10
_invalid_telemetry_lock = __import__("threading").Lock()
_invalid_telemetry: dict[str, tuple[int, float]] = {}


class AdminClientSourcePath(str, Enum):
    """Resolution path labels for structured telemetry (no raw addresses)."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_PEER_ONLY = "trusted_peer_only"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_TRUSTED_CHAIN = "forwarded_trusted_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    INVALID_FORWARDING = "invalid_forwarding"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class AdminClientSourceResult:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: AdminClientSourcePath


def parse_trusted_proxy_cidrs(spec: str | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDR allowlist; empty spec yields no trusted networks."""
    if not spec or not spec.strip():
        return ()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        networks.append(ipaddress.ip_network(token, strict=False))
    return tuple(networks)


def trusted_proxy_cidrs_for_settings(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return configured trusted proxy networks, with legacy env fallback."""
    if settings.admin_trusted_proxy_cidrs:
        return parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    if settings.admin_trust_proxy_headers:
        return parse_trusted_proxy_cidrs(",".join(DEFAULT_TRUSTED_PROXY_CIDRS))
    return ()


def cloudflare_proxy_cidrs_for_settings(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxy_cidrs(settings.admin_cloudflare_proxy_cidrs)


def normalize_ip_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject invalid input."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
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


def is_trusted_address(
    address: str | None,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def parse_x_forwarded_for(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    hops: list[str] = []
    for element in header_value.split(","):
        normalized = normalize_ip_address(element)
        if normalized is not None:
            hops.append(normalized)
    return tuple(hops)


_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"([^"]+)"|([^;,\s"]+))',
    re.IGNORECASE,
)


def parse_forwarded_header(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    hops: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        raw = next(value for value in match.groups() if value is not None)
        normalized = normalize_ip_address(raw)
        if normalized is not None:
            hops.append(normalized)
    return tuple(hops)


def _chain_exceeds_limit(chain: tuple[str, ...]) -> bool:
    return len(chain) > MAX_FORWARDING_CHAIN_LENGTH


def _is_skippable_proxy_hop(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return is_trusted_address(address, trusted_networks) or is_trusted_address(
        address, cloudflare_networks
    )


def _client_from_trusted_chain(
    chain: tuple[str, ...],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> str | None:
    if not chain or not trusted_networks:
        return None
    if not is_trusted_address(chain[-1], trusted_networks):
        return None
    remaining = list(chain)
    while remaining and _is_skippable_proxy_hop(
        remaining[-1], trusted_networks, cloudflare_networks
    ):
        remaining.pop()
    if not remaining:
        return None
    return remaining[-1]


def _cloudflare_hop_present(
    chain: tuple[str, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    return any(is_trusted_address(hop, cloudflare_networks) for hop in chain)


def _record_invalid_forwarding(path: AdminClientSourcePath) -> None:
    now = time.monotonic()
    key = path.value
    with _invalid_telemetry_lock:
        count, window_start = _invalid_telemetry.get(key, (0, now))
        if now - window_start >= _INVALID_TELEMETRY_WINDOW_SECONDS:
            count = 0
            window_start = now
        count += 1
        _invalid_telemetry[key] = (count, window_start)
        if count <= _INVALID_TELEMETRY_MAX_SAMPLES:
            _logger.info(
                "Admin login client source rejected forwarding data",
                extra={
                    "client_source_path": path.value,
                    "invalid_forwarding_sample": count,
                },
            )


def reset_invalid_forwarding_telemetry() -> None:
    """Clear sampled invalid-forwarding counters (tests only)."""
    with _invalid_telemetry_lock:
        _invalid_telemetry.clear()


def _emit_resolution_telemetry(path: AdminClientSourcePath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_path": path.value},
    )


def _peer_fallback(peer: str | None) -> str:
    return peer if peer is not None else "unknown"


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> AdminClientSourceResult:
    """Resolve the effective client source for admin login limiter buckets.

    Trust model (production: Cloudflare → Render proxy → Uvicorn):

    1. Forwarding headers are ignored unless the rightmost ``X-Forwarded-For``
       hop (or sole hop) belongs to ``ADMIN_TRUSTED_PROXY_CIDRS``.
    2. When that boundary is satisfied, walk the chain right-to-left, skipping
       trusted proxy hops; the rightmost remaining hop is the client source.
    3. When no valid ``X-Forwarded-For`` chain exists, ``Forwarded`` is parsed
       with the same rightmost-trusted-hop rule.
    4. ``CF-Connecting-IP`` is accepted only when a Cloudflare hop appears in
       the verified chain **and** ``ADMIN_CLOUDFLARE_PROXY_CIDRS`` is configured;
       it must agree with the chain-derived client or the request fails closed.
    5. Otherwise the direct ASGI peer address is used (fail closed).
    """
    trusted_networks = trusted_proxy_cidrs_for_settings(settings)
    cloudflare_networks = cloudflare_proxy_cidrs_for_settings(settings)
    peer = normalize_ip_address(request.client.host if request.client else None)

    xff_chain = parse_x_forwarded_for(request.headers.get("x-forwarded-for"))
    forwarded_chain = parse_forwarded_header(request.headers.get("forwarded"))

    if xff_chain:
        if _chain_exceeds_limit(xff_chain):
            _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
            _emit_resolution_telemetry(AdminClientSourcePath.INVALID_FORWARDING)
            return AdminClientSourceResult(
                _peer_fallback(peer), AdminClientSourcePath.INVALID_FORWARDING
            )

        if is_trusted_address(xff_chain[-1], trusted_networks):
            client = _client_from_trusted_chain(
                xff_chain, trusted_networks, cloudflare_networks
            )
            if client is None:
                _emit_resolution_telemetry(AdminClientSourcePath.TRUSTED_PEER_ONLY)
                return AdminClientSourceResult(
                    _peer_fallback(peer), AdminClientSourcePath.TRUSTED_PEER_ONLY
                )

            cf_header = normalize_ip_address(request.headers.get("cf-connecting-ip"))
            if cf_header and _cloudflare_hop_present(xff_chain, cloudflare_networks):
                if cf_header != client:
                    _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
                    _emit_resolution_telemetry(AdminClientSourcePath.INVALID_FORWARDING)
                    return AdminClientSourceResult(
                        _peer_fallback(peer), AdminClientSourcePath.INVALID_FORWARDING
                    )
                _emit_resolution_telemetry(AdminClientSourcePath.CF_CONNECTING_IP)
                return AdminClientSourceResult(client, AdminClientSourcePath.CF_CONNECTING_IP)

            _emit_resolution_telemetry(AdminClientSourcePath.XFF_TRUSTED_CHAIN)
            return AdminClientSourceResult(client, AdminClientSourcePath.XFF_TRUSTED_CHAIN)

        _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
        _emit_resolution_telemetry(AdminClientSourcePath.DIRECT_PEER)
        fallback = _peer_fallback(peer)
        return AdminClientSourceResult(fallback, AdminClientSourcePath.DIRECT_PEER)

    if forwarded_chain:
        if _chain_exceeds_limit(forwarded_chain):
            _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
            _emit_resolution_telemetry(AdminClientSourcePath.INVALID_FORWARDING)
            return AdminClientSourceResult(
                _peer_fallback(peer), AdminClientSourcePath.INVALID_FORWARDING
            )

        if is_trusted_address(forwarded_chain[-1], trusted_networks):
            client = _client_from_trusted_chain(
                forwarded_chain, trusted_networks, cloudflare_networks
            )
            if client is None:
                _emit_resolution_telemetry(AdminClientSourcePath.TRUSTED_PEER_ONLY)
                return AdminClientSourceResult(
                    _peer_fallback(peer), AdminClientSourcePath.TRUSTED_PEER_ONLY
                )
            _emit_resolution_telemetry(AdminClientSourcePath.FORWARDED_TRUSTED_CHAIN)
            return AdminClientSourceResult(client, AdminClientSourcePath.FORWARDED_TRUSTED_CHAIN)

        _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
        _emit_resolution_telemetry(AdminClientSourcePath.DIRECT_PEER)
        fallback = _peer_fallback(peer)
        return AdminClientSourceResult(fallback, AdminClientSourcePath.DIRECT_PEER)

    cf_header = normalize_ip_address(request.headers.get("cf-connecting-ip"))
    if cf_header:
        _record_invalid_forwarding(AdminClientSourcePath.INVALID_FORWARDING)
        _emit_resolution_telemetry(AdminClientSourcePath.DIRECT_PEER)
        fallback = _peer_fallback(peer)
        return AdminClientSourceResult(fallback, AdminClientSourcePath.DIRECT_PEER)

    if peer is None:
        _emit_resolution_telemetry(AdminClientSourcePath.MISSING_PEER)
        return AdminClientSourceResult("unknown", AdminClientSourcePath.MISSING_PEER)

    if trusted_networks and is_trusted_address(peer, trusted_networks):
        _emit_resolution_telemetry(AdminClientSourcePath.TRUSTED_PEER_ONLY)
        return AdminClientSourceResult(peer, AdminClientSourcePath.TRUSTED_PEER_ONLY)

    _emit_resolution_telemetry(AdminClientSourcePath.DIRECT_PEER)
    return AdminClientSourceResult(peer, AdminClientSourcePath.DIRECT_PEER)
