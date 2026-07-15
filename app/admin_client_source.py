"""Verified proxy trust for admin login client-source resolution."""

from __future__ import annotations

import ipaddress
import logging
import random
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

from fastapi import Request

from app.config import Settings
from app.trusted_networks import parse_trusted_networks

_logger = logging.getLogger(__name__)

# Conservative bound for comma-separated forwarding chains.
MAX_FORWARDING_CHAIN_LENGTH = 10

# Sampled operational telemetry for invalid forwarding attempts (no raw IPs).
_INVALID_FORWARDING_SAMPLE_RATE = 0.05
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0

_telemetry_lock = Lock()
_invalid_forwarding_attempts = 0
_last_invalid_forwarding_log_at = 0.0

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?:(?:\"([^\"]+)\")|([^;,\s]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a privacy-safe resolution path label."""

    source: str
    path: str


def normalize_client_ip(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 (including IPv4-mapped) or return None when invalid."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    host = text
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return None
        host = host[1:closing]
        remainder = text[closing + 1 :].strip()
        if remainder.startswith(":"):
            if not remainder[1:].isdigit():
                return None
    elif host.count(":") == 1 and "." in host:
        host, sep, port = host.partition(":")
        if sep != ":" or not port.isdigit():
            return None

    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def ip_in_trusted_networks(
    ip: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_client_ip(ip)
    if normalized is None:
        return False
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def _parse_x_forwarded_for(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [part.strip() for part in header_value.split(",")]


def _parse_forwarded_header(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    hosts: list[str] = []
    for entry in header_value.split(","):
        token = entry.strip()
        if not token:
            continue
        match = _FORWARDED_FOR_TOKEN.search(token)
        if match is None:
            continue
        host = match.group(1) or match.group(2) or ""
        host = host.strip()
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        hosts.append(host)
    return hosts


def _select_client_from_trusted_chain(
    chain: list[str],
    *,
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    trusted_edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping verified trusted hops."""
    trusted_networks = trusted_proxy_networks + trusted_edge_networks
    for raw_host in reversed(chain):
        normalized = normalize_client_ip(raw_host)
        if normalized is None:
            continue
        if ip_in_trusted_networks(normalized, trusted_networks):
            continue
        return normalized
    return None


def _trusted_edge_present(
    chain: list[str],
    *,
    trusted_edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not trusted_edge_networks:
        return False
    for raw_host in chain:
        normalized = normalize_client_ip(raw_host)
        if normalized is None:
            continue
        if ip_in_trusted_networks(normalized, trusted_edge_networks):
            return True
    return False


def _emit_source_resolution_telemetry(path: str) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={"client_source_path": path},
    )


def _record_invalid_forwarding_attempt() -> None:
    global _invalid_forwarding_attempts, _last_invalid_forwarding_log_at

    with _telemetry_lock:
        _invalid_forwarding_attempts += 1
        attempts = _invalid_forwarding_attempts
        now = time.monotonic()
        due = now - _last_invalid_forwarding_log_at >= _INVALID_FORWARDING_LOG_INTERVAL_SECONDS
        if not due:
            return
        if attempts == 1 or random.random() < _INVALID_FORWARDING_SAMPLE_RATE:
            _last_invalid_forwarding_log_at = now
            _invalid_forwarding_attempts = 0
            _logger.warning(
                "Admin login ignored invalid or untrusted forwarding headers",
                extra={"invalid_forwarding_sampled": True},
            )


def reset_client_source_telemetry_for_tests() -> None:
    """Reset sampled invalid-forwarding counters (tests only)."""
    global _invalid_forwarding_attempts, _last_invalid_forwarding_log_at

    with _telemetry_lock:
        _invalid_forwarding_attempts = 0
        _last_invalid_forwarding_log_at = 0.0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Internet client → Cloudflare edge → Render load balancer → Uvicorn``

    Forwarding headers are honored only when the immediate TCP peer is a member of
    ``ADMIN_TRUSTED_PROXY_IPS``. Header families follow one precedence rule:

    1. ``CF-Connecting-IP`` when a trusted edge hop is present in the forwarding chain
    2. ``X-Forwarded-For`` (right-to-left trusted-hop stripping)
    3. ``Forwarded`` ``for=`` values (same stripping logic)
    4. Immediate peer, then ``unknown`` when missing
    """
    immediate_peer = request.client.host if request.client is not None else None
    immediate_normalized = normalize_client_ip(immediate_peer)

    if immediate_normalized is None:
        _emit_source_resolution_telemetry("missing_peer")
        return ClientSourceResolution("unknown", "missing_peer")

    trusted_proxy_networks = settings.admin_trusted_proxy_networks
    trusted_edge_networks = settings.admin_trusted_edge_networks

    if not trusted_proxy_networks:
        _emit_source_resolution_telemetry("direct_peer")
        return ClientSourceResolution(immediate_normalized, "direct_peer")

    if not ip_in_trusted_networks(immediate_normalized, trusted_proxy_networks):
        _emit_source_resolution_telemetry("direct_peer")
        return ClientSourceResolution(immediate_normalized, "direct_peer")

    xff_chain = _parse_x_forwarded_for(request.headers.get("x-forwarded-for"))
    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded"))

    if (
        len(xff_chain) > MAX_FORWARDING_CHAIN_LENGTH
        or len(forwarded_chain) > MAX_FORWARDING_CHAIN_LENGTH
    ):
        _record_invalid_forwarding_attempt()
        _emit_source_resolution_telemetry("invalid_forwarding")
        return ClientSourceResolution(immediate_normalized, "invalid_forwarding")

    if any(not part for part in xff_chain) or any(not part for part in forwarded_chain):
        _record_invalid_forwarding_attempt()
        _emit_source_resolution_telemetry("invalid_forwarding")
        return ClientSourceResolution(immediate_normalized, "invalid_forwarding")

    chains_for_edge_check = xff_chain or forwarded_chain
    edge_verified = _trusted_edge_present(
        chains_for_edge_check,
        trusted_edge_networks=trusted_edge_networks,
    )

    if edge_verified:
        cf_connecting_ip = normalize_client_ip(request.headers.get("cf-connecting-ip"))
        if cf_connecting_ip is not None:
            _emit_source_resolution_telemetry("cf_connecting_ip")
            return ClientSourceResolution(cf_connecting_ip, "cf_connecting_ip")

    if xff_chain:
        resolved = _select_client_from_trusted_chain(
            xff_chain,
            trusted_proxy_networks=trusted_proxy_networks,
            trusted_edge_networks=trusted_edge_networks,
        )
        if resolved is not None:
            _emit_source_resolution_telemetry("xff_trusted_chain")
            return ClientSourceResolution(resolved, "xff_trusted_chain")

    if forwarded_chain:
        resolved = _select_client_from_trusted_chain(
            forwarded_chain,
            trusted_proxy_networks=trusted_proxy_networks,
            trusted_edge_networks=trusted_edge_networks,
        )
        if resolved is not None:
            _emit_source_resolution_telemetry("forwarded_header")
            return ClientSourceResolution(resolved, "forwarded_header")

    _emit_source_resolution_telemetry("trusted_peer_fallback")
    return ClientSourceResolution(immediate_normalized, "trusted_peer_fallback")


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized client source string used by admin login limiters."""
    return resolve_admin_login_client_source(request, settings).source
