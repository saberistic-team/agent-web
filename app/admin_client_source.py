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

from app.config import Settings, parse_cidr_list

_logger = logging.getLogger(__name__)

# Conservative bounds for forwarding header parsing.
_MAX_FORWARDED_CHAIN_LENGTH = 32
_MAX_FORWARDED_HEADER_LENGTH = 2048

# Re-export deployment defaults for tests and documentation parity.
from app.config import DEFAULT_TRUSTED_HOP_CIDRS, DEFAULT_TRUSTED_PROXY_CIDRS, parse_cidr_list

# Sampled telemetry for invalid forwarding attempts (no raw addresses).
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_last_invalid_forwarding_log_at = 0.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    TRUSTED_X_FORWARDED_FOR = "trusted_x_forwarded_for"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    UNTRUSTED_PEER_HEADERS_IGNORED = "untrusted_peer_headers_ignored"
    INVALID_FORWARDING_IGNORED = "invalid_forwarding_ignored"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source for limiter keying (no raw header material)."""

    source: str
    path: SourceResolutionPath


def _compiled_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _strip_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    if value.count(":") == 1 and "." in value:
        host, _port = value.rsplit(":", 1)
        if host.replace(".", "").isdigit() and _port.isdigit():
            return host
    return value


def normalize_ip(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 (including IPv4-mapped) to a canonical string."""
    if raw is None:
        return None
    candidate = _strip_port(raw.strip())
    if not candidate:
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address in network for network in networks)


def peer_is_trusted(peer_host: str | None, trusted_proxy_cidrs: tuple[str, ...]) -> bool:
    """Return whether the immediate TCP peer is inside the trusted proxy boundary."""
    if not peer_host or not trusted_proxy_cidrs:
        return False
    normalized = normalize_ip(peer_host)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return _address_in_networks(address, _compiled_networks(trusted_proxy_cidrs))


def _split_forwarded_chain(header_value: str) -> list[str]:
    if len(header_value) > _MAX_FORWARDED_HEADER_LENGTH:
        return []
    parts = [segment.strip() for segment in header_value.split(",")]
    if len(parts) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return parts


def _client_from_x_forwarded_for(
    header_value: str,
    *,
    trusted_hop_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk X-Forwarded-For right-to-left, skipping trusted proxy hops."""
    hops = _split_forwarded_chain(header_value)
    if not hops:
        return None

    for hop in reversed(hops):
        if not hop:
            continue
        normalized = normalize_ip(hop)
        if normalized is None:
            return None
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return None
        if _address_in_networks(address, trusted_hop_networks):
            continue
        return normalized
    return None


_FORWARDED_FOR_RE = re.compile(r'for="?([^;,\"]+)"?', re.IGNORECASE)


def _client_from_forwarded_header(
    header_value: str,
    *,
    trusted_hop_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Parse RFC 7239 Forwarded header entries right-to-left."""
    if len(header_value) > _MAX_FORWARDED_HEADER_LENGTH:
        return None
    entries = [entry.strip() for entry in header_value.split(",") if entry.strip()]
    if not entries or len(entries) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None

    for entry in reversed(entries):
        match = _FORWARDED_FOR_RE.search(entry)
        if match is None:
            return None
        candidate = match.group(1).strip()
        if candidate.lower() == "unknown":
            continue
        normalized = normalize_ip(candidate)
        if normalized is None:
            return None
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return None
        if _address_in_networks(address, trusted_hop_networks):
            continue
        return normalized
    return None


def _cloudflare_hop_verified(
    x_forwarded_for: str,
    *,
    cloudflare_hop_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    hops = _split_forwarded_chain(x_forwarded_for)
    for hop in hops:
        normalized = normalize_ip(hop)
        if normalized is None:
            continue
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            continue
        if _address_in_networks(address, cloudflare_hop_networks):
            return True
    return False


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _record_resolution_telemetry(
    path: SourceResolutionPath,
    *,
    invalid_forwarding: bool = False,
) -> None:
    global _last_invalid_forwarding_log_at
    extra: dict[str, object] = {"source_resolution_path": path.value}
    if invalid_forwarding:
        now = time.monotonic()
        if now - _last_invalid_forwarding_log_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _last_invalid_forwarding_log_at = now
        extra["invalid_forwarding_observed"] = True
    _logger.info("Admin login client source resolved", extra=extra)


def reset_source_resolution_telemetry() -> None:
    """Reset sampled telemetry state (tests only)."""
    global _last_invalid_forwarding_log_at
    _last_invalid_forwarding_log_at = 0.0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve limiter source identity with a verified trusted-proxy boundary.

    Production chain (documented): Client → Cloudflare edge → Render proxy → Uvicorn.

    Forwarding headers are honored only when the immediate TCP peer is inside
    ``ADMIN_TRUSTED_PROXY_CIDRS``. Untrusted peers always use the direct peer
    address so spoofed ``X-Forwarded-For``, ``Forwarded``, or ``CF-Connecting-IP``
    cannot influence limiter buckets.

    Header precedence when the peer is trusted:

    1. ``CF-Connecting-IP`` — only when a Cloudflare hop is present in
       ``X-Forwarded-For`` (prevents direct Render origin spoofing).
    2. ``X-Forwarded-For`` — right-to-left walk skipping trusted hop CIDRs.
    3. ``Forwarded`` — same right-to-left semantics.
    4. Trusted peer host — conservative fallback when forwarding data is absent
       or malformed.
    """
    peer_host = request.client.host if request.client is not None else None
    normalized_peer = normalize_ip(peer_host) or "unknown"

    trusted_proxy_cidrs = settings.admin_trusted_proxy_cidrs
    trusted_hop_cidrs = settings.admin_trusted_hop_cidrs
    trusted_hop_networks = _compiled_networks(trusted_hop_cidrs)
    cloudflare_hop_networks = _compiled_networks(settings.admin_cloudflare_hop_cidrs)

    if not peer_is_trusted(peer_host, trusted_proxy_cidrs):
        if _has_forwarding_headers(request):
            _record_resolution_telemetry(
                SourceResolutionPath.UNTRUSTED_PEER_HEADERS_IGNORED,
                invalid_forwarding=True,
            )
        else:
            _record_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(normalized_peer, SourceResolutionPath.DIRECT_PEER)

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    forwarded_header = request.headers.get("forwarded", "")

    if cf_connecting_ip and x_forwarded_for:
        cf_normalized = normalize_ip(cf_connecting_ip)
        if (
            cf_normalized is not None
            and _cloudflare_hop_verified(
                x_forwarded_for,
                cloudflare_hop_networks=cloudflare_hop_networks,
            )
        ):
            _record_resolution_telemetry(SourceResolutionPath.TRUSTED_CF_CONNECTING_IP)
            return ClientSourceResolution(
                cf_normalized,
                SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
            )

    if x_forwarded_for:
        xff_client = _client_from_x_forwarded_for(
            x_forwarded_for,
            trusted_hop_networks=trusted_hop_networks,
        )
        if xff_client is not None:
            _record_resolution_telemetry(SourceResolutionPath.TRUSTED_X_FORWARDED_FOR)
            return ClientSourceResolution(
                xff_client,
                SourceResolutionPath.TRUSTED_X_FORWARDED_FOR,
            )

    if forwarded_header:
        forwarded_client = _client_from_forwarded_header(
            forwarded_header,
            trusted_hop_networks=trusted_hop_networks,
        )
        if forwarded_client is not None:
            _record_resolution_telemetry(SourceResolutionPath.TRUSTED_FORWARDED)
            return ClientSourceResolution(
                forwarded_client,
                SourceResolutionPath.TRUSTED_FORWARDED,
            )

    if _has_forwarding_headers(request):
        _record_resolution_telemetry(
            SourceResolutionPath.INVALID_FORWARDING_IGNORED,
            invalid_forwarding=True,
        )
        fallback_path = SourceResolutionPath.INVALID_FORWARDING_IGNORED
    else:
        _record_resolution_telemetry(SourceResolutionPath.TRUSTED_PEER_FALLBACK)
        fallback_path = SourceResolutionPath.TRUSTED_PEER_FALLBACK

    return ClientSourceResolution(normalized_peer, fallback_path)
