"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Production chain: browser → Cloudflare edge → Render load balancer → Uvicorn.
# Cloudflare appends the connecting address to X-Forwarded-For, so the left-most
# value may remain attacker-controlled. Resolution therefore walks trusted hops
# from the right and only accepts vendor headers after the immediate peer matches
# ADMIN_TRUSTED_PROXY_CIDRS.
MAX_FORWARDING_CHAIN_LENGTH = 32
_REJECTED_FORWARDED_LOG_WINDOW_SECONDS = 60
_REJECTED_FORWARDED_LOG_MAX_PER_WINDOW = 10

_logger = logging.getLogger(__name__)
_rejected_forwarded_lock = Lock()
_rejected_forwarded_state = {"window_start": 0.0, "count": 0}

_FORWARDED_FOR_SEGMENT = re.compile(
    r"^\s*for=(?:\"([^\"]+)\"|([^;,\s]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source plus a privacy-safe resolution path label."""

    source: str
    path: str
    rejected_forwarded: bool = False


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs/addresses used for the proxy trust boundary."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 strings deterministically; return None when invalid."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host_part, _, remainder = candidate[1:].partition("]")
        if remainder.startswith(":"):
            candidate = host_part
        else:
            candidate = host_part

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def is_trusted_proxy_address(
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


def _split_forwarding_chain(raw: str) -> list[str]:
    parts = [segment.strip() for segment in raw.split(",")]
    return [part for part in parts if part][:MAX_FORWARDING_CHAIN_LENGTH]


def client_from_forwarding_chain(
    chain: str,
    *,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Return the right-most untrusted hop from a comma-separated forwarding chain."""
    hops = _split_forwarding_chain(chain)
    if not hops:
        return None

    for hop in reversed(hops):
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        if not is_trusted_proxy_address(normalized, trusted_networks):
            return normalized

    for hop in reversed(hops):
        normalized = normalize_client_address(hop)
        if normalized is not None:
            return normalized
    return None


def parse_forwarded_header(value: str) -> list[str]:
    """Extract ``for=`` values from an RFC 7239 Forwarded header."""
    addresses: list[str] = []
    for entry in value.split(","):
        match = _FORWARDED_FOR_SEGMENT.search(entry)
        if match is None:
            continue
        raw = match.group(1) or match.group(2) or ""
        normalized = normalize_client_address(raw)
        if normalized is not None:
            addresses.append(normalized)
    return addresses[:MAX_FORWARDING_CHAIN_LENGTH]


def client_from_forwarded_header(
    value: str,
    *,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    addresses = parse_forwarded_header(value)
    if not addresses:
        return None

    for address in reversed(addresses):
        if not is_trusted_proxy_address(address, trusted_networks):
            return address

    return addresses[-1]


def _direct_peer_source(request: Request) -> str:
    if request.client is None:
        return "unknown"
    normalized = normalize_client_address(request.client.host)
    if normalized is not None:
        return normalized
    peer = request.client.host.strip()
    return peer or "unknown"


def _has_forwarding_headers(request: Request) -> bool:
    header_names = (
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    )
    return any(request.headers.get(name, "").strip() for name in header_names)


def _emit_rejected_forwarded_telemetry(path: str) -> None:
    now = time.monotonic()
    with _rejected_forwarded_lock:
        window_start = _rejected_forwarded_state["window_start"]
        if now - window_start >= _REJECTED_FORWARDED_LOG_WINDOW_SECONDS:
            _rejected_forwarded_state["window_start"] = now
            _rejected_forwarded_state["count"] = 0
        if _rejected_forwarded_state["count"] >= _REJECTED_FORWARDED_LOG_MAX_PER_WINDOW:
            return
        _rejected_forwarded_state["count"] += 1
    _logger.info(
        "Admin login ignored untrusted forwarding headers",
        extra={"source_path": path},
    )


def _emit_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    if resolution.rejected_forwarded:
        _emit_rejected_forwarded_telemetry(resolution.path)
        return
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_path": resolution.path},
    )


def reset_client_source_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    with _rejected_forwarded_lock:
        _rejected_forwarded_state["window_start"] = 0.0
        _rejected_forwarded_state["count"] = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_source = _direct_peer_source(request)
    forwarding_present = _has_forwarding_headers(request)

    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(
            source=peer_source,
            path="direct_peer",
            rejected_forwarded=forwarding_present,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    trusted_networks = parse_trusted_proxy_networks(settings.admin_trusted_proxy_cidrs)
    if not trusted_networks:
        resolution = ClientSourceResolution(
            source=peer_source,
            path="unconfigured_trust_boundary",
            rejected_forwarded=forwarding_present,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    if not is_trusted_proxy_address(peer_source, trusted_networks):
        resolution = ClientSourceResolution(
            source=peer_source,
            path="untrusted_peer",
            rejected_forwarded=forwarding_present,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        normalized = normalize_client_address(cf_connecting_ip)
        if normalized is not None:
            resolution = ClientSourceResolution(
                source=normalized,
                path="trusted_cf_connecting_ip",
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        normalized = client_from_forwarding_chain(
            forwarded_for,
            trusted_networks=trusted_networks,
        )
        if normalized is not None:
            resolution = ClientSourceResolution(
                source=normalized,
                path="trusted_xff_chain",
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        normalized = client_from_forwarded_header(
            forwarded,
            trusted_networks=trusted_networks,
        )
        if normalized is not None:
            resolution = ClientSourceResolution(
                source=normalized,
                path="trusted_forwarded",
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    resolution = ClientSourceResolution(
        source=peer_source,
        path="trusted_peer_fallback",
        rejected_forwarded=forwarding_present,
    )
    _emit_resolution_telemetry(resolution)
    return resolution


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).source
