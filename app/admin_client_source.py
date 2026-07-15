"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative bounds for forwarded header parsing.
MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_FORWARDED_HEADER_BYTES = 2048

# Sampled operational telemetry for invalid forwarding attempts (no raw IPs).
_TELEMETRY_LOCK = Lock()
_TELEMETRY_LAST_LOGGED: dict[str, float] = {}
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 300.0


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled telemetry timestamps (tests only)."""
    with _TELEMETRY_LOCK:
        _TELEMETRY_LAST_LOGGED.clear()

# Production request chain (public edge → app process):
# Browser → Cloudflare (sets CF-Connecting-IP, may extend X-Forwarded-For)
#         → Render load balancer (immediate TCP peer to Uvicorn)
#         → Uvicorn (forwarded-allow-ips must match ADMIN_TRUSTED_PROXY_IPS)
#
# Header precedence when the immediate peer is a configured trusted proxy:
# 1. CF-Connecting-IP — Cloudflare client identity on the production path
# 2. Forwarded (RFC 7239) `for=` — first syntactically valid address
# 3. X-Forwarded-For — right-to-left walk skipping trusted proxy hops
# 4. Immediate peer address — fail-closed fallback when forwarding data is absent

_SOURCE_RESOLUTION_PATHS = frozenset(
    {
        "direct_peer",
        "missing_peer",
        "trusted_cf_connecting_ip",
        "trusted_forwarded",
        "trusted_xff_hops",
        "trusted_peer_fallback",
        "invalid_forwarding",
    }
)

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\")|(?:\[[^\]]+\](?::\d+)?)|(?:[A-Za-z0-9.:%_-]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the resolution path used."""

    source: str
    path: str


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted proxy CIDRs and host addresses."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            elif ":" in token:
                networks.append(ipaddress.ip_network(f"{token}/128", strict=False))
            else:
                networks.append(ipaddress.ip_network(f"{token}/32", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) or return None for invalid input."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith("[") and "]" in value:
        host, _, port = value[1:].partition("]")
        if port.startswith(":"):
            port = port[1:]
        if port and not port.isdigit():
            return None
        value = host
    elif value.count(":") == 1 and "." in value:
        host, sep, port = value.rpartition(":")
        if sep and port.isdigit():
            value = host
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def _address_in_trusted_networks(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = (request.client.host or "").strip()
    return host or None


def _has_forwarding_headers(request: Request) -> bool:
    header_names = (
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    )
    return any(request.headers.get(name, "").strip() for name in header_names)


def _log_source_resolution(path: str) -> None:
    if path not in _SOURCE_RESOLUTION_PATHS:
        return
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": path},
    )


def _log_untrusted_forwarding_attempt(reason: str) -> None:
    now = time.monotonic()
    with _TELEMETRY_LOCK:
        last_logged = _TELEMETRY_LAST_LOGGED.get(reason, 0.0)
        if now - last_logged < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _TELEMETRY_LAST_LOGGED[reason] = now
    _logger.info(
        "Admin login ignored untrusted forwarding headers",
        extra={"untrusted_forwarding_reason": reason},
    )


def _header_within_bounds(raw_value: str) -> bool:
    return 0 < len(raw_value) <= MAX_FORWARDED_HEADER_BYTES


def _parse_forwarded_for_header(
    raw_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    immediate_peer: str,
) -> str | None:
    if not _header_within_bounds(raw_value):
        return None
    hops = [part.strip() for part in raw_value.split(",")]
    if not hops or len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    if any(not hop for hop in hops):
        return None

    trusted_hops = {immediate_peer}
    for hop in hops:
        normalized = normalize_client_address(hop)
        if normalized is not None and _address_in_trusted_networks(normalized, trusted_networks):
            trusted_hops.add(normalized)

    for hop in reversed(hops):
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        if normalized in trusted_hops or _address_in_trusted_networks(
            normalized, trusted_networks
        ):
            continue
        return normalized
    return None


def _parse_rfc7239_forwarded_header(raw_value: str) -> str | None:
    if not _header_within_bounds(raw_value):
        return None
    entries = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not entries or len(entries) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    for entry in entries:
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            continue
        candidate = match.group("value").strip().strip('"')
        normalized = normalize_client_address(candidate)
        if normalized is not None:
            return normalized
    return None


def _resolve_from_trusted_peer(
    request: Request,
    *,
    immediate_peer: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> ClientSourceResolution:
    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    if cf_header:
        normalized = normalize_client_address(cf_header)
        if normalized is not None:
            _log_source_resolution("trusted_cf_connecting_ip")
            return ClientSourceResolution(normalized, "trusted_cf_connecting_ip")

    forwarded_header = request.headers.get("forwarded", "").strip()
    if forwarded_header:
        normalized = _parse_rfc7239_forwarded_header(forwarded_header)
        if normalized is not None:
            _log_source_resolution("trusted_forwarded")
            return ClientSourceResolution(normalized, "trusted_forwarded")

    xff_header = request.headers.get("x-forwarded-for", "").strip()
    if xff_header:
        normalized = _parse_forwarded_for_header(
            xff_header,
            trusted_networks=trusted_networks,
            immediate_peer=immediate_peer,
        )
        if normalized is not None:
            _log_source_resolution("trusted_xff_hops")
            return ClientSourceResolution(normalized, "trusted_xff_hops")
        _log_source_resolution("invalid_forwarding")
        return ClientSourceResolution(immediate_peer, "invalid_forwarding")

    _log_source_resolution("trusted_peer_fallback")
    return ClientSourceResolution(immediate_peer, "trusted_peer_fallback")


def _canonical_peer_identifier(peer_raw: str) -> str | None:
    """Return a stable limiter source identifier for the immediate TCP peer."""
    normalized_ip = normalize_client_address(peer_raw)
    if normalized_ip is not None:
        return normalized_ip
    stripped = peer_raw.strip()
    if not stripped:
        return None
    return stripped.lower()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_raw = _immediate_peer_host(request)
    if peer_raw is None:
        _log_source_resolution("missing_peer")
        return ClientSourceResolution("unknown", "missing_peer")

    peer = _canonical_peer_identifier(peer_raw)
    if peer is None:
        if _has_forwarding_headers(request):
            _log_untrusted_forwarding_attempt("malformed_peer")
        _log_source_resolution("missing_peer")
        return ClientSourceResolution("unknown", "missing_peer")

    trusted_networks = settings.admin_trusted_proxy_networks
    if not trusted_networks:
        if _has_forwarding_headers(request):
            _log_untrusted_forwarding_attempt("no_trusted_proxies_configured")
        _log_source_resolution("direct_peer")
        return ClientSourceResolution(peer, "direct_peer")

    if not _address_in_trusted_networks(peer, trusted_networks):
        if _has_forwarding_headers(request):
            _log_untrusted_forwarding_attempt("untrusted_immediate_peer")
        _log_source_resolution("direct_peer")
        return ClientSourceResolution(peer, "direct_peer")

    return _resolve_from_trusted_peer(
        request,
        immediate_peer=peer,
        trusted_networks=trusted_networks,
    )


def client_ip_from_resolution(resolution: ClientSourceResolution) -> str:
    """Return the limiter source string from a resolution result."""
    return resolution.source
