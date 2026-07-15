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
from app.proxy_trust_constants import DEFAULT_CLOUDFLARE_PROXY_CIDRS

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_FORWARDED_FOR_HEADER = "x-forwarded-for"
_CF_CONNECTING_IP_HEADER = "cf-connecting-ip"
_FORWARDED_HEADER = "forwarded"
_FORWARDED_FOR_RE = re.compile(r"for=(?:\"([^\"]+)\"|([^;,]+))", re.IGNORECASE)

# Sample invalid/untrusted forwarding telemetry (bounded operational signal).
_SPOOF_TELEMETRY_INTERVAL_SECONDS = 60.0
_spoof_telemetry_last_logged = 0.0
_spoof_telemetry_suppressed = 0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_HEADERS = "untrusted_headers"
    FORWARDED_CHAIN = "forwarded_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source plus a non-sensitive resolution path label."""

    source: str
    path: SourceResolutionPath
    untrusted_header_attempt: bool = False


@dataclass(frozen=True)
class _TrustedNetworks:
    proxy: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    cloudflare: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


def _compile_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _trusted_networks(settings: Settings) -> _TrustedNetworks | None:
    if not settings.admin_trusted_proxy_cidrs:
        return None
    cloudflare_cidrs = settings.admin_cloudflare_proxy_cidrs or DEFAULT_CLOUDFLARE_PROXY_CIDRS
    return _TrustedNetworks(
        proxy=_compile_networks(settings.admin_trusted_proxy_cidrs),
        cloudflare=_compile_networks(cloudflare_cidrs),
    )


def _strip_port(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if value.count(":") == 1 and "." in value:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            return host
    return value


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 deterministically; return None for invalid input."""
    candidate = _strip_port(raw)
    if not candidate:
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def _ip_in_networks(
    ip_value: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        parsed = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _parse_forwarded_for_header(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    if len(raw) > 4096:
        return None
    hops: list[str] = []
    for part in raw.split(","):
        if len(hops) >= MAX_FORWARDING_CHAIN_LENGTH:
            return None
        normalized = normalize_ip_address(part)
        if normalized is None:
            return None
        hops.append(normalized)
    if not hops:
        return None
    return hops


def _parse_forwarded_header(raw: str) -> list[str] | None:
    if not raw.strip() or len(raw) > 4096:
        return None
    hops: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        if len(hops) >= MAX_FORWARDING_CHAIN_LENGTH:
            return None
        candidate = match.group(1) or match.group(2) or ""
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return None
        hops.append(normalized)
    if not hops:
        return None
    return hops


def _direct_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    if not host:
        return None
    normalized = normalize_ip_address(host)
    return normalized or host


def _resolve_from_trusted_chain(
    hops: list[str],
    *,
    trusted: _TrustedNetworks,
    cf_connecting_ip: str | None,
) -> ClientSourceResolution:
    rightmost = hops[-1]
    if not _ip_in_networks(rightmost, trusted.proxy):
        return ClientSourceResolution(
            source=rightmost,
            path=SourceResolutionPath.UNTRUSTED_HEADERS,
            untrusted_header_attempt=bool(hops[:-1]),
        )

    remaining = list(hops)
    saw_cloudflare_hop = False
    while remaining and _ip_in_networks(remaining[-1], trusted.proxy):
        hop = remaining.pop()
        if _ip_in_networks(hop, trusted.cloudflare):
            saw_cloudflare_hop = True

    if not remaining:
        return ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.INVALID_FORWARDING,
            untrusted_header_attempt=True,
        )

    if len(remaining) > 1:
        return ClientSourceResolution(
            source=remaining[-1],
            path=SourceResolutionPath.UNTRUSTED_HEADERS,
            untrusted_header_attempt=True,
        )

    client_source = remaining[0]
    if saw_cloudflare_hop and cf_connecting_ip is not None:
        return ClientSourceResolution(
            source=cf_connecting_ip,
            path=SourceResolutionPath.CF_CONNECTING_IP,
        )
    return ClientSourceResolution(
        source=client_source,
        path=SourceResolutionPath.FORWARDED_CHAIN,
    )


def _maybe_log_untrusted_forwarding(resolution: ClientSourceResolution) -> None:
    if not resolution.untrusted_header_attempt:
        return
    global _spoof_telemetry_last_logged, _spoof_telemetry_suppressed
    now = time.monotonic()
    if now - _spoof_telemetry_last_logged < _SPOOF_TELEMETRY_INTERVAL_SECONDS:
        _spoof_telemetry_suppressed += 1
        return
    extra = {
        "resolution_path": resolution.path.value,
        "suppressed_since_last": _spoof_telemetry_suppressed,
    }
    _spoof_telemetry_last_logged = now
    _spoof_telemetry_suppressed = 0
    _logger.info("Admin login source forwarding rejected", extra=extra)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the request chain includes a
    verified trusted proxy hop (Render internal proxy, then Cloudflare edge in
    production). The rightmost ``X-Forwarded-For`` hop must be a trusted proxy;
    client identity is then derived by walking the chain from right to left.
    """
    trusted = _trusted_networks(settings)
    if trusted is None:
        peer = _direct_peer_host(request)
        if peer is None:
            return ClientSourceResolution(
                source="unknown",
                path=SourceResolutionPath.MISSING_PEER,
            )
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    forwarded_for = request.headers.get(_FORWARDED_FOR_HEADER, "")
    forwarded = request.headers.get(_FORWARDED_HEADER, "")
    hops = _parse_forwarded_for_header(forwarded_for)
    if hops is None and forwarded_for.strip():
        peer = _direct_peer_host(request)
        resolution = ClientSourceResolution(
            source=peer or "unknown",
            path=SourceResolutionPath.INVALID_FORWARDING,
            untrusted_header_attempt=True,
        )
        _maybe_log_untrusted_forwarding(resolution)
        return resolution

    if hops is None and forwarded.strip():
        hops = _parse_forwarded_header(forwarded)
        if hops is None:
            peer = _direct_peer_host(request)
            resolution = ClientSourceResolution(
                source=peer or "unknown",
                path=SourceResolutionPath.INVALID_FORWARDING,
                untrusted_header_attempt=True,
            )
            _maybe_log_untrusted_forwarding(resolution)
            return resolution

    if hops is None:
        peer = _direct_peer_host(request)
        if peer is None:
            return ClientSourceResolution(
                source="unknown",
                path=SourceResolutionPath.MISSING_PEER,
            )
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    cf_header = request.headers.get(_CF_CONNECTING_IP_HEADER, "")
    cf_connecting_ip = normalize_ip_address(cf_header) if cf_header.strip() else None
    if cf_connecting_ip is None and cf_header.strip():
        cf_connecting_ip = None

    resolution = _resolve_from_trusted_chain(
        hops,
        trusted=trusted,
        cf_connecting_ip=cf_connecting_ip,
    )
    if resolution.path in {
        SourceResolutionPath.UNTRUSTED_HEADERS,
        SourceResolutionPath.INVALID_FORWARDING,
    }:
        _maybe_log_untrusted_forwarding(resolution)
    return resolution


def reset_source_telemetry_state() -> None:
    """Reset spoof-telemetry sampling counters (tests only)."""
    global _spoof_telemetry_last_logged, _spoof_telemetry_suppressed
    _spoof_telemetry_last_logged = 0.0
    _spoof_telemetry_suppressed = 0
