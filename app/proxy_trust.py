"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

MISSING_SOURCE = "unknown"
MAX_FORWARD_CHAIN_LENGTH = 32
_TELEMETRY_WINDOW_SECONDS = 60
_TELEMETRY_MAX_EVENTS_PER_WINDOW = 20

_telemetry_lock = Lock()
_telemetry_window_start = 0.0
_telemetry_event_count = 0
_invalid_forwarded_total = 0


class SourceResolutionPath(StrEnum):
    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    INVALID_FORWARDED = "invalid_forwarded"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: SourceResolutionPath


def reset_proxy_trust_telemetry() -> None:
    """Clear telemetry counters (tests only)."""
    global _telemetry_window_start, _telemetry_event_count, _invalid_forwarded_total
    with _telemetry_lock:
        _telemetry_window_start = 0.0
        _telemetry_event_count = 0
        _invalid_forwarded_total = 0


def parse_trusted_proxy_networks(
    specs: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for spec in specs:
        cleaned = spec.strip()
        if not cleaned:
            continue
        try:
            if "/" in cleaned:
                networks.append(ipaddress.ip_network(cleaned, strict=False))
            else:
                host = cleaned.strip("[]")
                addr = ipaddress.ip_address(host)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(raw: str | None) -> str | None:
    """Return a canonical IP string or ``None`` when the value is not an IP."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            value = value[1:end]
    elif value.count(":") == 1 and "." in value:
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host

    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return addr.compressed


def peer_identity(raw: str | None) -> str:
    """Normalize a socket peer or fall back to a stable opaque label."""
    if raw is None:
        return MISSING_SOURCE
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized
    cleaned = raw.strip().lower()
    return cleaned if cleaned else MISSING_SOURCE


def is_trusted_proxy_address(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(ip in network for network in trusted_networks)


def parse_x_forwarded_for_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def resolve_x_forwarded_for_client(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    max_length: int = MAX_FORWARD_CHAIN_LENGTH,
) -> str | None:
    """Walk ``X-Forwarded-For`` right-to-left and return the first untrusted hop."""
    if not chain or len(chain) > max_length:
        return None

    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)

    for hop in reversed(normalized_chain):
        if not is_trusted_proxy_address(hop, trusted_networks):
            return hop
    return normalized_chain[0] if normalized_chain else None


def parse_forwarded_for_client(header_value: str) -> str | None:
    """Extract the first ``for=`` token from an RFC 7239 ``Forwarded`` header."""
    for entry in header_value.split(","):
        entry = entry.strip()
        for part in entry.split(";"):
            part = part.strip()
            if not part.lower().startswith("for="):
                continue
            token = part[4:].strip().strip('"')
            if token.lower() == "unknown":
                continue
            return normalize_client_address(token)
    return None


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _record_telemetry(path: SourceResolutionPath, *, invalid: bool = False) -> None:
    global _telemetry_window_start, _telemetry_event_count, _invalid_forwarded_total
    now = time.monotonic()
    with _telemetry_lock:
        if now - _telemetry_window_start >= _TELEMETRY_WINDOW_SECONDS:
            _telemetry_window_start = now
            _telemetry_event_count = 0
        if _telemetry_event_count >= _TELEMETRY_MAX_EVENTS_PER_WINDOW:
            return
        _telemetry_event_count += 1
        if invalid:
            _invalid_forwarded_total += 1

    _logger.info(
        "Admin login client source resolved",
        extra={
            "source_resolution_path": path.value,
            "invalid_forwarded": invalid,
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login source address for rate limiting.

    Production chain: browser -> Cloudflare edge -> Render load balancer -> Uvicorn.

    Forwarding headers are honored only when the immediate socket peer is a member
    of ``ADMIN_TRUSTED_PROXY_IPS``. Header precedence:

    1. ``CF-Connecting-IP`` when ``CF-Ray`` is also present (Cloudflare path)
    2. ``X-Forwarded-For`` parsed right-to-left with trusted-hop stripping
    3. RFC 7239 ``Forwarded``
    4. Immediate peer fallback
    """
    trusted_networks = parse_trusted_proxy_networks(settings.admin_trusted_proxy_ips)
    peer_raw = request.client.host if request.client is not None else None
    peer_source = peer_identity(peer_raw)

    if not settings.admin_trust_proxy_headers:
        if _has_forwarding_headers(request):
            _record_telemetry(SourceResolutionPath.INVALID_FORWARDED, invalid=True)
        result = ClientSourceResolution(
            source=peer_source,
            path=SourceResolutionPath.DIRECT_PEER,
        )
        _record_telemetry(result.path)
        return result

    peer_is_trusted = bool(
        peer_source != MISSING_SOURCE
        and trusted_networks
        and is_trusted_proxy_address(peer_source, trusted_networks)
    )

    if not peer_is_trusted:
        if _has_forwarding_headers(request):
            _record_telemetry(SourceResolutionPath.INVALID_FORWARDED, invalid=True)
        result = ClientSourceResolution(
            source=peer_source,
            path=SourceResolutionPath.DIRECT_PEER,
        )
        _record_telemetry(result.path)
        return result

    cf_connecting = request.headers.get("cf-connecting-ip")
    cf_ray = request.headers.get("cf-ray")
    if cf_connecting and cf_ray:
        cf_normalized = normalize_client_address(cf_connecting)
        if cf_normalized is not None:
            result = ClientSourceResolution(
                source=cf_normalized,
                path=SourceResolutionPath.CF_CONNECTING_IP,
            )
            _record_telemetry(result.path)
            return result

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        chain = parse_x_forwarded_for_chain(xff)
        if len(chain) > MAX_FORWARD_CHAIN_LENGTH:
            result = ClientSourceResolution(
                source=peer_source,
                path=SourceResolutionPath.INVALID_FORWARDED,
            )
            _record_telemetry(result.path, invalid=True)
            return result

        client = resolve_x_forwarded_for_client(chain, trusted_networks=trusted_networks)
        if client is not None:
            result = ClientSourceResolution(
                source=client,
                path=SourceResolutionPath.TRUSTED_XFF,
            )
            _record_telemetry(result.path)
            return result

        result = ClientSourceResolution(
            source=peer_source,
            path=SourceResolutionPath.INVALID_FORWARDED,
        )
        _record_telemetry(result.path, invalid=True)
        return result

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        client = parse_forwarded_for_client(forwarded)
        if client is not None:
            result = ClientSourceResolution(
                source=client,
                path=SourceResolutionPath.FORWARDED_HEADER,
            )
            _record_telemetry(result.path)
            return result

    if cf_connecting:
        _record_telemetry(SourceResolutionPath.INVALID_FORWARDED, invalid=True)

    result = ClientSourceResolution(
        source=peer_source,
        path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
    )
    _record_telemetry(result.path)
    return result
