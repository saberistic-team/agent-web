"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Cloudflare publishes these ranges; used only to verify the edge participated in
# the forwarding chain before honoring CF-Connecting-IP.
_CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
_CLOUDFLARE_IPV6_CIDRS: tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

MAX_FORWARD_CHAIN_LENGTH = 32
_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\")|\S+)",
    re.IGNORECASE,
)

_logger = logging.getLogger(__name__)
_untrusted_forwarding_last_logged = 0.0
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without retaining raw forwarding data."""

    source: str
    path: str
    header_families: tuple[str, ...] = ()


def _parse_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        candidate = cidr.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _cloudflare_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse_networks(_CLOUDFLARE_IPV4_CIDRS + _CLOUDFLARE_IPV6_CIDRS)


def _strip_port(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    if trimmed.startswith("["):
        end = trimmed.find("]")
        if end != -1:
            return trimmed[1:end]
    if trimmed.count(":") == 1 and "." in trimmed:
        host, _port = trimmed.rsplit(":", 1)
        if host and _port.isdigit():
            return host
    return trimmed


def normalize_client_address(value: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically for limiter digests."""
    candidate = _strip_port(value.strip().strip('"'))
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
    value: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(value)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _parse_forward_chain(header_value: str) -> list[str]:
    if not header_value or len(header_value) > 4096:
        return []
    hops: list[str] = []
    for raw_part in header_value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = normalize_client_address(part)
        if normalized is None:
            return []
        hops.append(normalized)
        if len(hops) > MAX_FORWARD_CHAIN_LENGTH:
            return []
    return hops


def _parse_forwarded_header(header_value: str) -> list[str]:
    if not header_value or len(header_value) > 4096:
        return []
    hops: list[str] = []
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            return []
        token = match.group("value").strip().strip('"')
        if token.lower() == "unknown":
            continue
        normalized = normalize_client_address(token)
        if normalized is None:
            return []
        hops.append(normalized)
        if len(hops) > MAX_FORWARD_CHAIN_LENGTH:
            return []
    return hops


def _trusted_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse_networks(settings.admin_trusted_proxy_cidrs)


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    normalized = normalize_client_address(request.client.host)
    if normalized is not None:
        return normalized
    host = request.client.host.strip()
    return host or None


def _strip_trailing_trusted_hops(
    hops: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> list[str]:
    remaining = list(hops)
    while remaining and _ip_in_networks(remaining[-1], trusted_networks):
        remaining.pop()
    return remaining


def _cloudflare_verified_in_chain(hops: Iterable[str]) -> bool:
    cf_networks = _cloudflare_networks()
    return any(_ip_in_networks(hop, cf_networks) for hop in hops)


def _client_from_forward_chain(
    hops: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not hops:
        return None
    remaining = _strip_trailing_trusted_hops(hops, trusted_networks)
    if not remaining:
        return None
    return remaining[-1]


def _xff_client_trusted(
    hops: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    *,
    cf_connecting_ip: str | None,
) -> str | None:
    client = _client_from_forward_chain(hops, trusted_networks)
    if client is None:
        return None
    if len(hops) == 1 and not _cloudflare_verified_in_chain(hops):
        if cf_connecting_ip is not None and cf_connecting_ip == client:
            return client
        return None
    return client


def _header_families_present(request: Request) -> tuple[str, ...]:
    families: list[str] = []
    if request.headers.get("x-forwarded-for"):
        families.append("x_forwarded_for")
    if request.headers.get("forwarded"):
        families.append("forwarded")
    if request.headers.get("cf-connecting-ip"):
        families.append("cf_connecting_ip")
    return tuple(families)


def _log_resolution(path: str, header_families: tuple[str, ...]) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={
            "client_source_path": path,
            "forwarding_header_families": list(header_families),
        },
    )


def _log_untrusted_forwarding(header_families: tuple[str, ...]) -> None:
    global _untrusted_forwarding_last_logged
    if not header_families:
        return
    now = time.monotonic()
    if now - _untrusted_forwarding_last_logged < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _untrusted_forwarding_last_logged = now
    _logger.warning(
        "Admin login ignored forwarding headers from untrusted peer",
        extra={"forwarding_header_families": list(header_families)},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (saberistic.com): browser → Cloudflare → Render load
    balancer → Uvicorn. Forwarding headers are honored only when the immediate
    TCP peer is a configured trusted proxy. Parsed chains are walked from the
    right, stripping trusted proxy hops, so attacker-controlled leftmost
    ``X-Forwarded-For`` values cannot mint fresh limiter buckets.

    Header precedence when the immediate peer is trusted:

    1. ``CF-Connecting-IP`` after a Cloudflare edge address appears in the
       ``X-Forwarded-For`` chain (direct origin access cannot spoof this).
    2. ``X-Forwarded-For`` right-to-left with trailing trusted hops removed.
    3. RFC 7239 ``Forwarded`` ``for=`` tokens with the same stripping rules.

    When proxy trust is disabled (local dev/tests), only the immediate peer is
    used and all forwarding headers are ignored.
    """
    header_families = _header_families_present(request)
    peer = _immediate_peer(request)

    if not settings.admin_trust_proxy_headers:
        source = peer or "unknown"
        resolution = ClientSourceResolution(
            source=source,
            path="direct_peer",
            header_families=header_families,
        )
        if header_families:
            _log_untrusted_forwarding(header_families)
        _log_resolution(resolution.path, resolution.header_families)
        return resolution

    trusted_networks = _trusted_networks(settings)
    if peer is None or not _ip_in_networks(peer, trusted_networks):
        if header_families:
            _log_untrusted_forwarding(header_families)
        resolution = ClientSourceResolution(
            source=peer or "unknown",
            path="untrusted_peer",
            header_families=header_families,
        )
        _log_resolution(resolution.path, resolution.header_families)
        return resolution

    xff_hops = _parse_forward_chain(request.headers.get("x-forwarded-for", ""))

    cf_candidate = normalize_client_address(request.headers.get("cf-connecting-ip", ""))
    if cf_candidate and xff_hops and _cloudflare_verified_in_chain(xff_hops):
        resolution = ClientSourceResolution(
            source=cf_candidate,
            path="cf_connecting_ip",
            header_families=header_families,
        )
        _log_resolution(resolution.path, resolution.header_families)
        return resolution

    xff_client = _xff_client_trusted(
        xff_hops,
        trusted_networks,
        cf_connecting_ip=cf_candidate,
    )
    if xff_client is not None:
        resolution = ClientSourceResolution(
            source=xff_client,
            path="x_forwarded_for",
            header_families=header_families,
        )
        _log_resolution(resolution.path, resolution.header_families)
        return resolution

    forwarded_hops = _parse_forwarded_header(request.headers.get("forwarded", ""))
    forwarded_client = _client_from_forward_chain(forwarded_hops, trusted_networks)
    if forwarded_client is not None:
        resolution = ClientSourceResolution(
            source=forwarded_client,
            path="forwarded",
            header_families=header_families,
        )
        _log_resolution(resolution.path, resolution.header_families)
        return resolution

    if header_families:
        _log_untrusted_forwarding(header_families)
    resolution = ClientSourceResolution(
        source="unknown",
        path="missing_forwarding",
        header_families=header_families,
    )
    _log_resolution(resolution.path, resolution.header_families)
    return resolution
