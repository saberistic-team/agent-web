"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

MAX_FORWARD_CHAIN_LENGTH = 32

# Bounded telemetry for ignored forwarding attempts (no raw addresses).
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_untrusted_forwarding_log_lock = threading.Lock()
_last_untrusted_forwarding_log_monotonic = 0.0

_logger = logging.getLogger(__name__)

_FORWARDING_HEADER_NAMES = frozenset(
    {
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    }
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a privacy-safe telemetry path label."""

    source: str
    path: str


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for admin login rate limiting."""
    return resolve_admin_login_client_source_detail(request, settings).source


def resolve_admin_login_client_source_detail(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve client source and record which resolution path was used."""
    peer = request.client.host if request.client is not None else None
    peer_normalized = normalize_client_address(peer) if peer is not None else None
    peer_source = peer_normalized
    if peer_source is None and peer is not None:
        stripped_peer = peer.strip()
        peer_source = stripped_peer or None

    if peer_source is None:
        resolution = ClientSourceResolution(source="unknown", path="missing_peer")
        _log_resolution(resolution)
        return resolution

    trusted_networks = parse_networks(settings.admin_trusted_proxy_cidrs)
    xff_chain = parse_forward_chain(request.headers.get("x-forwarded-for", ""))
    if not _verified_trusted_proxy_path(
        peer_source,
        xff_chain,
        trusted_networks=trusted_networks,
    ):
        if _has_forwarding_headers(request):
            _maybe_log_untrusted_forwarding("untrusted_peer_forwarding_ignored")
        resolution = ClientSourceResolution(source=peer_source, path="direct_peer")
        _log_resolution(resolution)
        return resolution

    edge_networks = parse_networks(settings.admin_edge_proxy_cidrs)

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_candidate = normalize_client_address(cf_header) if cf_header else None
    if (
        cf_candidate is not None
        and edge_networks
        and _cloudflare_path_proven(xff_chain, edge_networks)
    ):
        resolution = ClientSourceResolution(
            source=cf_candidate,
            path="cf_connecting_ip_verified",
        )
        _log_resolution(resolution)
        return resolution

    if xff_chain:
        resolved = _resolve_from_trusted_chain(
            xff_chain,
            trusted_networks=trusted_networks,
            edge_networks=edge_networks,
        )
        if resolved is not None:
            resolution = ClientSourceResolution(
                source=resolved,
                path="trusted_xff_right",
            )
            _log_resolution(resolution)
            return resolution
        _maybe_log_untrusted_forwarding("invalid_x_forwarded_for_ignored")

    forwarded_chain = parse_forwarded_header(request.headers.get("forwarded", ""))
    if forwarded_chain:
        resolved = _resolve_from_trusted_chain(
            forwarded_chain,
            trusted_networks=trusted_networks,
            edge_networks=edge_networks,
        )
        if resolved is not None:
            resolution = ClientSourceResolution(
                source=resolved,
                path="trusted_forwarded_header",
            )
            _log_resolution(resolution)
            return resolution
        _maybe_log_untrusted_forwarding("invalid_forwarded_header_ignored")

    if _has_forwarding_headers(request):
        _maybe_log_untrusted_forwarding("untrusted_forwarding_fallback")

    resolution = ClientSourceResolution(
        source=peer_source,
        path="trusted_peer_fallback",
    )
    _log_resolution(resolution)
    return resolution


def normalize_client_address(value: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    if text.startswith("[") and "]" in text:
        host, _, remainder = text.partition("]")
        text = host[1:]
        if remainder.startswith(":") and remainder[1:].isdigit():
            pass
    elif text.count(":") == 1 and "." in text:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            text = host

    if "%" in text:
        return None

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv6Address):
        return address.compressed
    return str(address)


@lru_cache(maxsize=16)
def parse_networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        candidate = cidr.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            else:
                address = ipaddress.ip_address(candidate)
                networks.append(
                    ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def address_in_networks(
    address: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def parse_forward_chain(header_value: str) -> tuple[str, ...]:
    """Parse and normalize a comma-separated forwarding chain (left-to-right order)."""
    if not header_value:
        return ()
    elements = header_value.split(",")
    if len(elements) > MAX_FORWARD_CHAIN_LENGTH:
        return ()
    normalized: list[str] = []
    for element in elements:
        if not element.strip():
            continue
        candidate = normalize_client_address(element)
        if candidate is None:
            return ()
        normalized.append(candidate)
    return tuple(normalized)


_FORWARDED_FOR_RE = re.compile(
    r"""for=(?:"\[([^\]]+)\](?::\d+)?"|([^;,\s"]+))""",
    re.IGNORECASE,
)


def parse_forwarded_header(header_value: str) -> tuple[str, ...]:
    """Parse RFC 7239 ``Forwarded`` hop values into normalized client addresses."""
    if not header_value:
        return ()
    hop_values = _split_forwarded_hops(header_value)
    if len(hop_values) > MAX_FORWARD_CHAIN_LENGTH:
        return ()
    normalized: list[str] = []
    for hop in hop_values:
        match = _FORWARDED_FOR_RE.search(hop)
        if match is None:
            return ()
        raw = match.group(1) or match.group(2) or ""
        candidate = normalize_client_address(raw)
        if candidate is None:
            return ()
        normalized.append(candidate)
    return tuple(normalized)


def _split_forwarded_hops(header_value: str) -> list[str]:
    hops: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in header_value:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
            continue
        if char == "," and not in_quotes:
            hop = "".join(current).strip()
            if hop:
                hops.append(hop)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        hops.append(tail)
    return hops


def _resolve_from_trusted_chain(
    chain: tuple[str, ...],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping trusted proxy hops."""
    if not chain:
        return None
    trusted_or_edge = tuple(trusted_networks) + tuple(edge_networks)
    for candidate in reversed(chain):
        if address_in_networks(candidate, trusted_or_edge):
            continue
        return candidate
    return None


def _cloudflare_path_proven(
    xff_chain: tuple[str, ...],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not xff_chain or not edge_networks:
        return False
    return any(address_in_networks(hop, edge_networks) for hop in xff_chain)


def _verified_trusted_proxy_path(
    peer_source: str,
    xff_chain: tuple[str, ...],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """Return True when the request arrived through a configured trusted proxy hop.

    Uvicorn ``--proxy-headers`` may rewrite ``request.client`` to the leftmost
    ``X-Forwarded-For`` value when the TCP peer is trusted. Accept that rewrite
    only when the rightmost chain hop is also a trusted proxy address.
    """
    if not trusted_networks:
        return False
    if address_in_networks(peer_source, trusted_networks):
        return True
    if len(xff_chain) < 2:
        return False
    rightmost = xff_chain[-1]
    if not address_in_networks(rightmost, trusted_networks):
        return False
    return peer_source == xff_chain[0]


def _has_forwarding_headers(request: Request) -> bool:
    lowered = {name.lower() for name in request.headers.keys()}
    return bool(lowered & _FORWARDING_HEADER_NAMES)


def _log_resolution(resolution: ClientSourceResolution) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"resolution_path": resolution.path},
    )


def _maybe_log_untrusted_forwarding(reason: str) -> None:
    global _last_untrusted_forwarding_log_monotonic
    now = time.monotonic()
    with _untrusted_forwarding_log_lock:
        if now - _last_untrusted_forwarding_log_monotonic < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _last_untrusted_forwarding_log_monotonic = now
    _logger.info(
        "Admin login forwarding headers ignored",
        extra={"resolution_path": reason},
    )


def reset_untrusted_forwarding_telemetry_for_tests() -> None:
    """Reset rate-limited forwarding telemetry (tests only)."""
    global _last_untrusted_forwarding_log_monotonic
    with _untrusted_forwarding_log_lock:
        _last_untrusted_forwarding_log_monotonic = 0.0
    parse_networks.cache_clear()
