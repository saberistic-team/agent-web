"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Render private-network ranges for the platform load balancer → app hop.
# Keep in sync with ``render.yaml`` ``--forwarded-allow-ips`` and
# ``ADMIN_TRUSTED_PROXY_CIDRS`` (enforced by deployment tests).
PRODUCTION_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "100.64.0.0/10",
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_INVALID_TELEMETRY_WINDOW_SECONDS = 60
_INVALID_TELEMETRY_MAX_SAMPLES = 10

_invalid_telemetry_lock = threading.Lock()
_invalid_telemetry: dict[str, tuple[int, float]] = {}

_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"([^"]+)"|([^;,\s"]+))',
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Telemetry-only labels for how client source was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_PEER_ONLY = "trusted_peer_only"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_TRUSTED_CHAIN = "forwarded_trusted_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    INVALID_FORWARDING = "invalid_forwarding"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting forwarding metadata."""

    address: str
    path: SourceResolutionPath


def parse_trusted_proxy_cidrs(
    spec: str | None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
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


def trusted_proxy_cidrs_for_settings(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return configured trusted proxy networks, with legacy env fallback."""
    if settings.admin_trusted_proxy_cidrs:
        return parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    if settings.admin_trust_proxy_headers:
        return parse_trusted_proxy_cidrs(",".join(PRODUCTION_TRUSTED_PROXY_CIDRS))
    return ()


def cloudflare_proxy_cidrs_for_settings(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxy_cidrs(settings.admin_cloudflare_proxy_cidrs)


def uvicorn_forwarded_allow_ips_arg(settings: Settings | None = None) -> str:
    """Return the comma-separated CIDR string for Uvicorn ``--forwarded-allow-ips``."""
    if settings is not None and settings.admin_trusted_proxy_cidrs.strip():
        return settings.admin_trusted_proxy_cidrs.strip()
    return ",".join(PRODUCTION_TRUSTED_PROXY_CIDRS)


def proxy_trust_enabled(settings: Settings) -> bool:
    """True when forwarding headers may be consulted for admin login sources."""
    return settings.admin_trust_proxy_headers and bool(trusted_proxy_cidrs_for_settings(settings))


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject invalid input."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        host_part = candidate[1 : candidate.index("]")]
        remainder = candidate[candidate.index("]") + 1 :]
        if remainder.startswith(":"):
            port = remainder[1:]
            if not port.isdigit():
                return None
        elif remainder:
            return None
        candidate = host_part
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
    normalized = normalize_client_address(address)
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
        normalized = normalize_client_address(element)
        if normalized is not None:
            hops.append(normalized)
    return tuple(hops)


def parse_forwarded_header(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    hops: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        raw = next(value for value in match.groups() if value is not None)
        normalized = normalize_client_address(raw)
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
    if not chain:
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


def _peer_identity(immediate_peer: str | None) -> str:
    normalized = normalize_client_address(immediate_peer)
    if normalized is not None:
        return normalized
    raw = (immediate_peer or "").strip()
    if not raw:
        return "unknown"
    return raw.lower()


def _peer_fallback(peer: str | None) -> str:
    return peer if peer is not None else "unknown"


def _record_invalid_forwarding(path: SourceResolutionPath) -> None:
    now = __import__("time").monotonic()
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
                    "source_resolution_path": path.value,
                    "invalid_forwarding_sample": count,
                },
            )


def reset_proxy_trust_telemetry() -> None:
    """Clear sampled invalid-forwarding counters (tests only)."""
    with _invalid_telemetry_lock:
        _invalid_telemetry.clear()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for shared limiter buckets.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Client → Cloudflare edge → Render load balancer → Uvicorn → FastAPI``

    Forwarding headers are consulted only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_CIDRS``. The left-most ``X-Forwarded-For`` value is
    never trusted directly; hops are evaluated from right to left, skipping
    configured proxy and Cloudflare ranges.
    """
    trusted_networks = trusted_proxy_cidrs_for_settings(settings)
    cloudflare_networks = cloudflare_proxy_cidrs_for_settings(settings)
    immediate_peer = request.client.host if request.client else None
    peer = _peer_identity(immediate_peer)

    if peer == "unknown" and (request.client is None or not immediate_peer or not immediate_peer.strip()):
        return ClientSourceResolution("unknown", SourceResolutionPath.MISSING_PEER)

    if not proxy_trust_enabled(settings):
        return ClientSourceResolution(_peer_fallback(peer), SourceResolutionPath.DIRECT_PEER)

    if not is_trusted_address(peer, trusted_networks):
        had_forwarding_headers = any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        )
        if had_forwarding_headers:
            _record_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResolution(_peer_fallback(peer), SourceResolutionPath.DIRECT_PEER)

    xff_chain = parse_x_forwarded_for(request.headers.get("x-forwarded-for"))
    forwarded_chain = parse_forwarded_header(request.headers.get("forwarded"))

    if xff_chain:
        if _chain_exceeds_limit(xff_chain):
            _record_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
            return ClientSourceResolution(
                _peer_fallback(peer), SourceResolutionPath.INVALID_FORWARDING
            )

        client = _client_from_trusted_chain(
            xff_chain, trusted_networks, cloudflare_networks
        )
        if client is None:
            return ClientSourceResolution(
                _peer_fallback(peer), SourceResolutionPath.TRUSTED_PEER_ONLY
            )

        cf_header = normalize_client_address(request.headers.get("cf-connecting-ip"))
        if cf_header and _cloudflare_hop_present(xff_chain, cloudflare_networks):
            if cf_header != client:
                _record_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
                return ClientSourceResolution(
                    _peer_fallback(peer), SourceResolutionPath.INVALID_FORWARDING
                )
            return ClientSourceResolution(client, SourceResolutionPath.CF_CONNECTING_IP)

        return ClientSourceResolution(client, SourceResolutionPath.XFF_TRUSTED_CHAIN)

    if forwarded_chain:
        if _chain_exceeds_limit(forwarded_chain):
            _record_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
            return ClientSourceResolution(
                _peer_fallback(peer), SourceResolutionPath.INVALID_FORWARDING
            )

        client = _client_from_trusted_chain(
            forwarded_chain, trusted_networks, cloudflare_networks
        )
        if client is None:
            return ClientSourceResolution(
                _peer_fallback(peer), SourceResolutionPath.TRUSTED_PEER_ONLY
            )
        return ClientSourceResolution(client, SourceResolutionPath.FORWARDED_TRUSTED_CHAIN)

    cf_header = normalize_client_address(request.headers.get("cf-connecting-ip"))
    if cf_header:
        _record_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResolution(_peer_fallback(peer), SourceResolutionPath.DIRECT_PEER)

    return ClientSourceResolution(_peer_fallback(peer), SourceResolutionPath.TRUSTED_PEER_ONLY)
