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

MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_FORWARDING_HEADER_LENGTH = 2048
_UNKNOWN_SOURCE = "unknown"

# Sampled operational telemetry for ignored forwarding headers (no raw IPs).
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_untrusted_forwarding_log_lock = Lock()
_last_untrusted_forwarding_log_at = 0.0

_logger = logging.getLogger(__name__)

_FORWARDED_FOR_RE = re.compile(
    r"for=(?:\"?\[([^\]]+)\]|\"?([^;,\"]+)\"?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and non-sensitive telemetry metadata."""

    source: str
    path: str
    ignored_untrusted_forwarding: bool = False


def parse_trusted_proxy_networks(cidrs: Iterable[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse configured trusted-proxy CIDRs; skip invalid entries."""
    networks: list[ipaddress._BaseNetwork] = []
    for raw in cidrs:
        entry = raw.strip()
        if not entry:
            continue
        try:
            if "/" not in entry:
                addr = ipaddress.ip_address(entry)
                networks.append(
                    ipaddress.ip_network(f"{entry}/{addr.max_prefixlen}", strict=False)
                )
            else:
                networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_ip(value: str) -> str | None:
    """Return a deterministic normalized IP string or ``None`` when invalid."""
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > MAX_FORWARDING_HEADER_LENGTH:
        return None

    host_part = candidate
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host_part = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":"):
            port = remainder[1:]
            if not port.isdigit():
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host_part, sep, port = candidate.partition(":")
        if sep and not port.isdigit():
            return None

    try:
        addr = ipaddress.ip_address(host_part)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return addr.compressed.lower()


def immediate_peer_host(request: Request) -> str | None:
    """Return the raw TCP peer host before any application-side rewriting."""
    if request.client is None:
        return None
    host = request.client.host.strip()
    return host or None


def is_trusted_proxy_host(host: str, trusted_networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    normalized = normalize_client_ip(host)
    if normalized is None:
        return False
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(addr in network for network in trusted_networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return []
    parts = [part.strip() for part in header_value.split(",")]
    if len(parts) > MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return parts


def _client_from_x_forwarded_for(
    header_value: str,
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
) -> str | None:
    parts = _split_forwarding_chain(header_value)
    if not parts:
        return None
    for part in reversed(parts):
        normalized = normalize_client_ip(part)
        if normalized is None:
            continue
        if not is_trusted_proxy_host(normalized, trusted_networks):
            return normalized
    return None


def _client_from_forwarded_header(
    header_value: str,
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
) -> str | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        raw = match.group(1) or match.group(2) or ""
        normalized = normalize_client_ip(raw.strip())
        if normalized is None:
            continue
        if not is_trusted_proxy_host(normalized, trusted_networks):
            return normalized
    return None


def _has_forwarding_headers(request: Request) -> bool:
    header_names = (
        "cf-connecting-ip",
        "x-forwarded-for",
        "forwarded",
    )
    return any(request.headers.get(name, "").strip() for name in header_names)


def _maybe_log_ignored_untrusted_forwarding(path: str) -> None:
    global _last_untrusted_forwarding_log_at
    now = time.monotonic()
    with _untrusted_forwarding_log_lock:
        if now - _last_untrusted_forwarding_log_at < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _last_untrusted_forwarding_log_at = now
    _logger.info(
        "Admin login client source ignored untrusted forwarding headers",
        extra={"source_resolution_path": path},
    )


def reset_untrusted_forwarding_telemetry_for_tests() -> None:
    """Clear sampled telemetry state (tests only)."""
    global _last_untrusted_forwarding_log_at
    with _untrusted_forwarding_log_lock:
        _last_untrusted_forwarding_log_at = 0.0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``settings.admin_trusted_proxy_cidrs``. Header precedence (trusted peers only):

    1. ``CF-Connecting-IP``
    2. ``Forwarded`` (first ``for=`` token)
    3. ``X-Forwarded-For`` (right-to-left, skipping trusted hops)
    4. Immediate peer fallback
    """
    trusted_networks = parse_trusted_proxy_networks(settings.admin_trusted_proxy_cidrs)
    peer = immediate_peer_host(request)
    peer_normalized = normalize_client_ip(peer) if peer is not None else None

    if not trusted_networks or peer_normalized is None:
        if _has_forwarding_headers(request):
            _maybe_log_ignored_untrusted_forwarding("direct_peer")
        source = peer_normalized or _UNKNOWN_SOURCE
        return ClientSourceResolution(
            source=source,
            path="unknown" if peer_normalized is None else "direct_peer",
            ignored_untrusted_forwarding=_has_forwarding_headers(request),
        )

    if not is_trusted_proxy_host(peer_normalized, trusted_networks):
        if _has_forwarding_headers(request):
            _maybe_log_ignored_untrusted_forwarding("direct_peer")
        return ClientSourceResolution(
            source=peer_normalized,
            path="direct_peer",
            ignored_untrusted_forwarding=_has_forwarding_headers(request),
        )

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        normalized = normalize_client_ip(cf_connecting_ip)
        if normalized is not None:
            return ClientSourceResolution(source=normalized, path="cf_connecting_ip")

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        normalized = _client_from_forwarded_header(forwarded, trusted_networks)
        if normalized is not None:
            return ClientSourceResolution(source=normalized, path="forwarded")

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        normalized = _client_from_x_forwarded_for(x_forwarded_for, trusted_networks)
        if normalized is not None:
            return ClientSourceResolution(source=normalized, path="x_forwarded_for")

    return ClientSourceResolution(source=peer_normalized, path="trusted_peer_fallback")


def deployment_source_trust_mode(settings: Settings) -> str:
    """Non-sensitive label for deployment verification (health checks)."""
    if settings.admin_trusted_proxy_cidrs:
        return "trusted_proxy_boundary"
    return "direct_peer_only"
