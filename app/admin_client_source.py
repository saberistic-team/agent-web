"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_TELEMETRY_MAX_PER_WINDOW = 10
_TELEMETRY_WINDOW_SECONDS = 60.0

# Production request chain: public client → Cloudflare edge → Render load balancer → Uvicorn.
# Render's immediate peer is a private-network reverse proxy; Cloudflare appends connecting
# addresses to X-Forwarded-For rather than replacing the header.
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
)

RESOLUTION_DIRECT_PEER = "direct_peer"
RESOLUTION_PROXY_TRUST_DISABLED = "proxy_trust_disabled"
RESOLUTION_MISSING_PEER = "missing_peer"
RESOLUTION_TRUSTED_CF_CONNECTING = "trusted_cf_connecting"
RESOLUTION_TRUSTED_XFF_CHAIN = "trusted_xff_chain"
RESOLUTION_TRUSTED_FORWARDED_CHAIN = "trusted_forwarded_chain"
RESOLUTION_TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
RESOLUTION_MALFORMED_FORWARDING = "malformed_forwarding"
RESOLUTION_UNTRUSTED_FORWARDING_ATTEMPT = "untrusted_forwarding_attempt"

_FORWARDED_FOR_TOKEN = re.compile(r"for=(?P<value>[^;,\s]+)", re.IGNORECASE)

_telemetry_window_start = 0.0
_telemetry_count = 0


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved limiter source identity without persisting raw forwarding data."""

    address: str
    resolution_path: str


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 addresses deterministically."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder.startswith(":") and remainder[1:].isdigit():
            value = host
        else:
            value = host
    elif value.count(":") == 1 and "." in value:
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host

    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def parse_trusted_proxy_networks(spec: str | Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs into networks; invalid entries are ignored."""
    if isinstance(spec, str):
        tokens = [part.strip() for part in spec.split(",")]
    else:
        tokens = [part.strip() for part in spec if part.strip()]

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in tokens:
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


@lru_cache(maxsize=8)
def _cached_trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not spec.strip():
        return parse_trusted_proxy_networks(DEFAULT_TRUSTED_PROXY_CIDRS)
    return parse_trusted_proxy_networks(spec)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _cached_trusted_networks(settings.admin_trusted_proxy_ips)


def is_trusted_proxy_address(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _has_forwarding_headers(request: Request) -> bool:
    header_names = (
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    )
    return any(request.headers.get(name) for name in header_names)


def _parse_x_forwarded_for(header_value: str) -> list[str]:
    hops: list[str] = []
    for token in header_value.split(","):
        normalized = normalize_client_address(token)
        if normalized is not None:
            hops.append(normalized)
    return hops


def _parse_forwarded_header(header_value: str) -> list[str]:
    hops: list[str] = []
    for match in _FORWARDED_FOR_TOKEN.finditer(header_value):
        raw_value = match.group("value").strip().strip('"')
        if raw_value.lower() == "unknown":
            continue
        if raw_value.startswith("["):
            raw_value = raw_value.strip("[]")
        normalized = normalize_client_address(raw_value)
        if normalized is not None:
            hops.append(normalized)
    return hops


def _walk_trusted_chain_right_to_left(
    chain: list[str],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain:
        return None
    for hop in reversed(chain):
        if not is_trusted_proxy_address(hop, networks):
            return hop
    return None


def _emit_resolution_telemetry(resolution_path: str) -> None:
    global _telemetry_window_start, _telemetry_count
    now = time.monotonic()
    if now - _telemetry_window_start >= _TELEMETRY_WINDOW_SECONDS:
        _telemetry_window_start = now
        _telemetry_count = 0
    if _telemetry_count >= _TELEMETRY_MAX_PER_WINDOW:
        return
    _telemetry_count += 1
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": resolution_path},
    )


def reset_client_source_telemetry_for_tests() -> None:
    """Reset rate-limited telemetry counters (tests only)."""
    global _telemetry_window_start, _telemetry_count
    _telemetry_window_start = 0.0
    _telemetry_count = 0


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResult:
    """Resolve the effective client source for admin login rate limiting.

    Trust model:

    1. When proxy trust is disabled (local dev/tests), only the immediate peer is used.
    2. When enabled, forwarding headers are honored only if the immediate peer is in
       ``ADMIN_TRUSTED_PROXY_IPS``.
    3. For trusted peers, ``CF-Connecting-IP`` is preferred when present (Render only
       receives this from Cloudflare on the public hostname).
    4. Otherwise ``X-Forwarded-For`` is parsed right-to-left, skipping trusted hops.
    5. ``Forwarded`` is used only when ``X-Forwarded-For`` does not yield a client.
    6. Untrusted peers ignore all forwarding headers.
    """
    peer_raw = _peer_host(request)
    peer = normalize_client_address(peer_raw)
    if peer is None:
        _emit_resolution_telemetry(RESOLUTION_MISSING_PEER)
        return ClientSourceResult("unknown", RESOLUTION_MISSING_PEER)

    if not settings.admin_trust_proxy_headers:
        if _has_forwarding_headers(request):
            _emit_resolution_telemetry(RESOLUTION_PROXY_TRUST_DISABLED)
        return ClientSourceResult(peer, RESOLUTION_PROXY_TRUST_DISABLED)

    networks = trusted_proxy_networks(settings)
    if not is_trusted_proxy_address(peer, networks):
        if _has_forwarding_headers(request):
            _emit_resolution_telemetry(RESOLUTION_UNTRUSTED_FORWARDING_ATTEMPT)
        return ClientSourceResult(peer, RESOLUTION_DIRECT_PEER)

    cf_header = request.headers.get("cf-connecting-ip")
    cf_client = normalize_client_address(cf_header)
    if cf_client is not None:
        _emit_resolution_telemetry(RESOLUTION_TRUSTED_CF_CONNECTING)
        return ClientSourceResult(cf_client, RESOLUTION_TRUSTED_CF_CONNECTING)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        xff_hops = _parse_x_forwarded_for(xff_header)
        if len(xff_hops) > MAX_FORWARDING_CHAIN_LENGTH:
            _emit_resolution_telemetry(RESOLUTION_MALFORMED_FORWARDING)
            return ClientSourceResult(peer, RESOLUTION_MALFORMED_FORWARDING)
        chain = [*xff_hops, peer]
        if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
            _emit_resolution_telemetry(RESOLUTION_MALFORMED_FORWARDING)
            return ClientSourceResult(peer, RESOLUTION_MALFORMED_FORWARDING)
        client = _walk_trusted_chain_right_to_left(chain, networks)
        if client is not None:
            _emit_resolution_telemetry(RESOLUTION_TRUSTED_XFF_CHAIN)
            return ClientSourceResult(client, RESOLUTION_TRUSTED_XFF_CHAIN)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_hops = _parse_forwarded_header(forwarded_header)
        if len(forwarded_hops) > MAX_FORWARDING_CHAIN_LENGTH:
            _emit_resolution_telemetry(RESOLUTION_MALFORMED_FORWARDING)
            return ClientSourceResult(peer, RESOLUTION_MALFORMED_FORWARDING)
        chain = [*forwarded_hops, peer]
        if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
            _emit_resolution_telemetry(RESOLUTION_MALFORMED_FORWARDING)
            return ClientSourceResult(peer, RESOLUTION_MALFORMED_FORWARDING)
        client = _walk_trusted_chain_right_to_left(chain, networks)
        if client is not None:
            _emit_resolution_telemetry(RESOLUTION_TRUSTED_FORWARDED_CHAIN)
            return ClientSourceResult(client, RESOLUTION_TRUSTED_FORWARDED_CHAIN)

    _emit_resolution_telemetry(RESOLUTION_TRUSTED_PEER_FALLBACK)
    return ClientSourceResult(peer, RESOLUTION_TRUSTED_PEER_FALLBACK)


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).address


def proxy_trust_health_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification payload for /health."""
    networks = trusted_proxy_networks(settings)
    return {
        "proxy_header_trust_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_network_count": len(networks),
        "uvicorn_forwarded_allow_ips_configured": bool(
            settings.uvicorn_forwarded_allow_ips.strip()
        ),
    }
