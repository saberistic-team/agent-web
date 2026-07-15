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

_LOGGER = logging.getLogger(__name__)

_UNKNOWN_SOURCE = "unknown"
_MAX_FORWARDED_CHAIN_LENGTH = 32
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_TELEMETRY_MAX_BURST = 5

PATH_DIRECT_PEER = "direct_peer"
PATH_FORWARDED_CHAIN = "forwarded_chain"
PATH_CF_CONNECTING_IP = "cf_connecting_ip"
PATH_FORWARDED_HEADER = "forwarded_header"
PATH_MISSING_PEER = "missing_peer"
PATH_INVALID_FORWARDED = "invalid_forwarded"
PATH_UNTRUSTED_FORWARDED = "untrusted_forwarded"

_FORWARDED_FOR_VALUE = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\"|\[[^\]]+\]|[^;,]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceResolution:
    """Resolved limiter source and the resolution path used."""

    source: str
    path: str


class _TelemetrySampler:
    """Rate-limited structured telemetry without raw addresses or headers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_emitted: dict[str, float] = {}
        self._burst: dict[str, int] = {}

    def emit(self, path: str, *, untrusted_attempt: bool = False) -> None:
        key = f"{path}:untrusted={untrusted_attempt}"
        now = time.monotonic()
        with self._lock:
            last = self._last_emitted.get(key, 0.0)
            burst = self._burst.get(key, 0)
            if now - last < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
                if burst >= _TELEMETRY_MAX_BURST:
                    return
                self._burst[key] = burst + 1
            else:
                self._last_emitted[key] = now
                self._burst[key] = 1
        extra: dict[str, object] = {"source_resolution_path": path}
        if untrusted_attempt:
            extra["untrusted_forwarded_attempt"] = True
            _LOGGER.info(
                "Admin login source resolution ignored forwarding headers",
                extra=extra,
            )
        else:
            _LOGGER.debug("Admin login source resolution", extra=extra)


_telemetry = _TelemetrySampler()


def parse_trusted_networks(
    values: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in values:
        candidate = raw.strip()
        if not candidate:
            continue
        if "/" not in candidate:
            address = normalize_client_address(candidate)
            if address is None:
                continue
            parsed = ipaddress.ip_address(address)
            prefix = 32 if parsed.version == 4 else 128
            networks.append(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
            continue
        networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


@lru_cache(maxsize=16)
def trusted_networks_for_cidrs(
    cidrs: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_networks(cidrs)


def reset_trusted_network_cache() -> None:
    """Clear parsed CIDR cache (tests only)."""
    trusted_networks_for_cidrs.cache_clear()


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses; strip ports and IPv4-mapped IPv6."""
    candidate = raw.strip().strip('"').strip("'")
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        host, _, port = candidate[1:].partition("]")
        if port.startswith(":"):
            candidate = host
        else:
            candidate = host
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        parsed = ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        return None
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if parsed.version == 4:
        return str(parsed)
    return parsed.compressed


def address_in_trusted_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not networks:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    normalized = normalize_client_address(request.client.host)
    if normalized is not None:
        return normalized
    raw_host = request.client.host.strip()
    return raw_host or None


def _parse_x_forwarded_for_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _parse_forwarded_header(header_value: str) -> str | None:
    """Parse the first ``for=`` value from an RFC 7239 ``Forwarded`` header."""
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_VALUE.search(entry.strip())
        if match is None:
            continue
        value = match.group("value").strip().strip('"').strip("'")
        if value.lower() == "unknown":
            continue
        normalized = normalize_client_address(value)
        if normalized is not None:
            return normalized
    return None


def _resolve_from_forwarded_chain(
    chain_hosts: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk ``X-Forwarded-For`` right-to-left; first untrusted hop is the client."""
    if not chain_hosts:
        return None
    for host_raw in reversed(chain_hosts):
        normalized = normalize_client_address(host_raw)
        if normalized is None:
            return None
        if not address_in_trusted_networks(normalized, trusted_networks):
            return normalized
    return normalize_client_address(chain_hosts[0])


def _cloudflare_edge_verified(
    chain_hosts: list[str],
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not edge_networks:
        return False
    for host_raw in chain_hosts:
        normalized = normalize_client_address(host_raw)
        if normalized and address_in_trusted_networks(normalized, edge_networks):
            return True
    return False


def _resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> SourceResolution:
    peer = _immediate_peer_host(request)
    if peer is None:
        return SourceResolution(_UNKNOWN_SOURCE, PATH_MISSING_PEER)

    trusted_proxies = settings.admin_trusted_proxy_networks
    edge_networks = settings.admin_trusted_edge_networks
    forwarding_present = any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "cf-connecting-ip", "forwarded")
    )

    if not trusted_proxies or not address_in_trusted_networks(peer, trusted_proxies):
        if forwarding_present:
            return SourceResolution(peer, PATH_UNTRUSTED_FORWARDED)
        return SourceResolution(peer, PATH_DIRECT_PEER)

    chain_trusted_networks = trusted_proxies + edge_networks
    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _parse_x_forwarded_for_chain(xff_raw) if xff_raw else []
    if len(xff_chain) > _MAX_FORWARDED_CHAIN_LENGTH:
        return SourceResolution(peer, PATH_INVALID_FORWARDED)

    cf_raw = request.headers.get("cf-connecting-ip", "")
    if (
        cf_raw
        and edge_networks
        and _cloudflare_edge_verified(xff_chain, edge_networks)
    ):
        cf_normalized = normalize_client_address(cf_raw)
        if cf_normalized is not None:
            return SourceResolution(cf_normalized, PATH_CF_CONNECTING_IP)

    if xff_chain:
        client = _resolve_from_forwarded_chain(xff_chain, chain_trusted_networks)
        if client is None:
            return SourceResolution(peer, PATH_INVALID_FORWARDED)
        return SourceResolution(client, PATH_FORWARDED_CHAIN)

    forwarded_raw = request.headers.get("forwarded", "")
    if forwarded_raw:
        parsed = _parse_forwarded_header(forwarded_raw)
        if parsed is None:
            return SourceResolution(peer, PATH_INVALID_FORWARDED)
        return SourceResolution(parsed, PATH_FORWARDED_HEADER)

    if forwarding_present:
        return SourceResolution(peer, PATH_UNTRUSTED_FORWARDED)

    return SourceResolution(peer, PATH_DIRECT_PEER)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Return the normalized client source used by the admin login limiter."""
    resolution = _resolve_admin_login_client_source(request, settings)
    _telemetry.emit(
        resolution.path,
        untrusted_attempt=resolution.path == PATH_UNTRUSTED_FORWARDED,
    )
    return resolution.source


def resolve_admin_login_client_source_detail(
    request: Request,
    settings: Settings,
) -> SourceResolution:
    """Return source plus resolution path (tests and diagnostics)."""
    resolution = _resolve_admin_login_client_source(request, settings)
    _telemetry.emit(
        resolution.path,
        untrusted_attempt=resolution.path == PATH_UNTRUSTED_FORWARDED,
    )
    return resolution


def reset_source_resolution_telemetry() -> None:
    """Clear telemetry sampler state (tests only)."""
    with _telemetry._lock:
        _telemetry._last_emitted.clear()
        _telemetry._burst.clear()
