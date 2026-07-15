"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative upper bound for comma-separated forwarding chains.
_MAX_FORWARDING_CHAIN_LENGTH = 32

# Sampled operational telemetry for rejected/untrusted forwarding attempts.
_TELEMETRY_LOCK = threading.Lock()
_TELEMETRY_LAST_EMIT = 0.0
_TELEMETRY_REJECT_COUNTS: dict[str, int] = {}
_TELEMETRY_EMIT_INTERVAL_SECONDS = 60.0

# Render internal load balancers and loopback (production immediate peer).
_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
    "fc00::/7",
)

# Cloudflare published egress ranges (https://www.cloudflare.com/ips-v4 / ips-v6).
_CLOUDFLARE_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
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
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

DEFAULT_PRODUCTION_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    *_RENDER_TRUSTED_PROXY_CIDRS,
    *_CLOUDFLARE_TRUSTED_PROXY_CIDRS,
)

_UNKNOWN_SOURCE = "unknown"

_FORWARDED_FOR_VALUE = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\")|\S+)(?:;|$)",
    re.IGNORECASE,
)


class ClientSourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    FORWARDED_HEADER = "forwarded_header"
    CF_CONNECTING_IP = "cf_connecting_ip"
    UNKNOWN = "unknown"
    UNTRUSTED_HEADERS_REJECTED = "untrusted_headers_rejected"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: ClientSourceResolutionPath


def parse_trusted_proxy_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured-CIDR strings into network objects (invalid entries skipped)."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            _logger.warning(
                "Ignoring invalid admin trusted proxy CIDR",
                extra={"cidr_length": len(candidate)},
            )
    return tuple(networks)


def configured_trusted_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    """Return effective trusted-proxy-CIDR list for the active deployment profile."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    if settings.admin_trust_proxy_headers:
        return DEFAULT_PRODUCTION_TRUSTED_PROXY_CIDRS
    return ()


def _strip_port(candidate: str) -> str:
    text = candidate.strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")]
    if text.count(":") == 1 and "." in text:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host
    return text


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. IPv4-mapped) or return None when invalid."""
    candidate = _strip_port(raw.strip().strip('"'))
    if not candidate:
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _ip_in_trusted_networks(
    ip_text: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(ip_text)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    if not header_value.strip():
        return []
    elements = [part.strip() for part in header_value.split(",")]
    if len(elements) > _MAX_FORWARDING_CHAIN_LENGTH:
        return []
    if any(not element for element in elements):
        return []
    return elements


def _trusted_boundary_reached(
    *,
    peer_host: str | None,
    chain: list[str],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if peer_host and _ip_in_trusted_networks(peer_host, networks):
        return True
    if not chain:
        return False
    rightmost = chain[-1]
    if not _ip_in_trusted_networks(rightmost, networks):
        return False
    if peer_host is None:
        return True
    peer_normalized = normalize_ip_address(peer_host)
    leftmost_normalized = normalize_ip_address(chain[0])
    # Accept trusted right-most hops only when the TCP peer matches the left-most
    # XFF entry (ProxyHeaders rewrite) — not when an untrusted relay forwards a
    # synthetic chain unchanged.
    return (
        peer_normalized is not None
        and leftmost_normalized is not None
        and peer_normalized == leftmost_normalized
    )


def _client_from_trusted_forwarding_chain(
    chain: list[str],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain or len(chain) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None

    remaining = list(chain)
    while remaining and _ip_in_trusted_networks(remaining[-1], networks):
        remaining.pop()

    if not remaining:
        return None

    normalized = normalize_ip_address(remaining[-1])
    if normalized is None:
        return None
    if _ip_in_trusted_networks(normalized, networks):
        return None
    return normalized


def _parse_forwarded_header_for_value(header_value: str) -> str | None:
    if not header_value.strip():
        return None
    first_entry = header_value.split(",")[0].strip()
    match = _FORWARDED_FOR_VALUE.search(first_entry)
    if match is None:
        return None
    value = match.group("value").strip().strip('"')
    if value.casefold() == "unknown":
        return None
    return value


def _record_untrusted_header_telemetry(reason: str) -> None:
    global _TELEMETRY_LAST_EMIT
    now = time.monotonic()
    with _TELEMETRY_LOCK:
        _TELEMETRY_REJECT_COUNTS[reason] = _TELEMETRY_REJECT_COUNTS.get(reason, 0) + 1
        if now - _TELEMETRY_LAST_EMIT < _TELEMETRY_EMIT_INTERVAL_SECONDS:
            return
        counts = dict(_TELEMETRY_REJECT_COUNTS)
        _TELEMETRY_REJECT_COUNTS.clear()
        _TELEMETRY_LAST_EMIT = now
    _logger.info(
        "Admin login source forwarding rejected (sampled)",
        extra={"rejection_counts": counts},
    )


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    raw = request.client.host.strip()
    if not raw:
        return None
    normalized = normalize_ip_address(raw)
    return normalized if normalized is not None else raw


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for shared rate limiting.

    Trust model (production: Cloudflare → Render load balancer → Uvicorn):

    1. When the immediate peer is not a configured trusted proxy *and* the
       right-most ``X-Forwarded-For`` hop is not trusted, ignore all forwarding
       headers and use the direct peer address.
    2. When the trusted boundary is satisfied, walk ``X-Forwarded-For`` from
       the right, skipping trusted proxy hops; the right-most remaining hop is
       the client (so attacker-prepended left-most values are ignored).
    3. ``Forwarded`` and ``CF-Connecting-IP`` are consulted only after the same
       boundary check, with precedence ``X-Forwarded-For`` → ``Forwarded`` →
       ``CF-Connecting-IP``.
    """
    networks = parse_trusted_proxy_networks(configured_trusted_proxy_cidrs(settings))
    peer = _peer_host(request)

    if not networks:
        if peer is not None:
            return ClientSourceResolution(source=peer, path=ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=ClientSourceResolutionPath.UNKNOWN)

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarding_chain(xff_header)
    if xff_header.strip() and not xff_chain:
        _record_untrusted_header_telemetry("malformed_x_forwarded_for")
        if peer is not None:
            return ClientSourceResolution(
                source=peer,
                path=ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED,
            )
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED,
        )

    if _trusted_boundary_reached(peer_host=peer, chain=xff_chain, networks=networks):
        if xff_chain:
            client = _client_from_trusted_forwarding_chain(xff_chain, networks)
            if client is not None:
                return ClientSourceResolution(
                    source=client,
                    path=ClientSourceResolutionPath.FORWARDED_CHAIN,
                )
            _record_untrusted_header_telemetry("invalid_forwarding_chain")
            return ClientSourceResolution(
                source=peer or _UNKNOWN_SOURCE,
                path=ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED,
            )

        forwarded_header = request.headers.get("forwarded", "")
        forwarded_value = _parse_forwarded_header_for_value(forwarded_header)
        if forwarded_value is not None:
            client = normalize_ip_address(forwarded_value)
            if client is not None and not _ip_in_trusted_networks(client, networks):
                return ClientSourceResolution(
                    source=client,
                    path=ClientSourceResolutionPath.FORWARDED_HEADER,
                )
            _record_untrusted_header_telemetry("invalid_forwarded_header")

        cf_header = request.headers.get("cf-connecting-ip", "")
        if cf_header.strip():
            client = normalize_ip_address(cf_header)
            if client is not None and not _ip_in_trusted_networks(client, networks):
                return ClientSourceResolution(
                    source=client,
                    path=ClientSourceResolutionPath.CF_CONNECTING_IP,
                )
            _record_untrusted_header_telemetry("invalid_cf_connecting_ip")

        if peer is not None:
            return ClientSourceResolution(source=peer, path=ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=ClientSourceResolutionPath.UNKNOWN)

    # Uvicorn ProxyHeaders applies the same right-to-left trusted-hop extraction to
    # scope["client"] when --forwarded-allow-ips matches. Accept when consistent.
    if xff_chain and peer is not None and _ip_in_trusted_networks(xff_chain[-1], networks):
        client = _client_from_trusted_forwarding_chain(xff_chain, networks)
        if client is not None and client == peer:
            return ClientSourceResolution(
                source=peer,
                path=ClientSourceResolutionPath.FORWARDED_CHAIN,
            )

    had_untrusted_headers = bool(
        xff_header.strip()
        or request.headers.get("forwarded", "").strip()
        or request.headers.get("cf-connecting-ip", "").strip()
    )
    if had_untrusted_headers:
        _record_untrusted_header_telemetry("untrusted_peer_with_forwarding_headers")

    if peer is not None:
        return ClientSourceResolution(
            source=peer,
            path=ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED
            if had_untrusted_headers
            else ClientSourceResolutionPath.DIRECT_PEER,
        )
    return ClientSourceResolution(
        source=_UNKNOWN_SOURCE,
        path=ClientSourceResolutionPath.UNTRUSTED_HEADERS_REJECTED
        if had_untrusted_headers
        else ClientSourceResolutionPath.UNKNOWN,
    )


def proxy_trust_health_summary(settings: Settings) -> dict[str, bool | str]:
    """Non-sensitive deployment verification fields for /health."""
    cidrs = configured_trusted_proxy_cidrs(settings)
    return {
        "trusted_proxy_cidrs_configured": bool(cidrs),
        "uvicorn_forwarded_allow_ips_configured": bool(
            settings.uvicorn_forwarded_allow_ips.strip()
        ),
        "legacy_admin_trust_proxy_headers": settings.admin_trust_proxy_headers,
    }
