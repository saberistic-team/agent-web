"""Verified-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Conservative cap on comma-separated forwarding chains (Cloudflare → Render → app).
_MAX_FORWARDED_CHAIN_LENGTH = 32

# Sampled operational telemetry for invalid/untrusted forwarding attempts.
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0

_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_last_untrusted_telemetry_at = 0.0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the resolution path used."""

    source: str
    path: str


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted proxy CIDRs/hosts from configuration."""
    from app.config import _parse_trusted_proxy_networks

    return _parse_trusted_proxy_networks(raw)


def normalize_client_source(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject invalid input."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    host = candidate
    if candidate.startswith("[") and "]" in candidate:
        host = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        # IPv4 with port, e.g. 203.0.113.1:443
        host = candidate.rsplit(":", 1)[0]

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _address_in_trusted_networks(
    address: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host.strip() or None


def _split_forwarded_for(raw: str) -> list[str]:
    if len(raw) > 4096:
        return []
    parts = [segment.strip() for segment in raw.split(",")]
    if len(parts) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return [part for part in parts if part]


def _parse_forwarded_header(raw: str) -> list[str]:
    """Extract ``for=`` identifiers from RFC 7239 ``Forwarded`` (rightmost first)."""
    if len(raw) > 4096:
        return []
    addresses: list[str] = []
    for entry in raw.split(","):
        for match in re.finditer(
            r'(?:^|;)\s*for=(?:"([^"]+)"|\[([^\]]+)\]|([^",;\s]+))',
            entry,
            re.I,
        ):
            candidate = (match.group(1) or match.group(2) or match.group(3) or "").strip()
            if candidate.lower() == "unknown":
                continue
            normalized = normalize_client_source(candidate)
            if normalized is not None:
                addresses.append(normalized)
    if len(addresses) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return addresses


def _right_to_left_untrusted_client(
    chain: list[str],
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Walk a forwarding chain from the trusted edge toward the client."""
    for address in reversed(chain):
        normalized = normalize_client_source(address)
        if normalized is None:
            continue
        if _address_in_trusted_networks(normalized, trusted_networks):
            continue
        return normalized
    return None


def _resolve_from_forwarded_headers(
    request: Request,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, str]:
    """Header precedence: ``Forwarded`` → ``X-Forwarded-For`` → ``CF-Connecting-IP``."""
    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        chain = _parse_forwarded_header(forwarded)
        client = _right_to_left_untrusted_client(chain, trusted_networks)
        if client is not None:
            return client, "forwarded_rfc7239"

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        chain = _split_forwarded_for(xff)
        if not chain:
            return None, "malformed_forwarded"
        client = _right_to_left_untrusted_client(chain, trusted_networks)
        if client is not None:
            return client, "forwarded_xff"

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip:
        normalized = normalize_client_source(cf_connecting_ip)
        if normalized is not None and not _address_in_trusted_networks(
            normalized, trusted_networks
        ):
            return normalized, "forwarded_cf_connecting_ip"

    return None, "no_forwarded_client"


def reset_untrusted_forwarding_telemetry() -> None:
    """Clear sampled telemetry state (tests only)."""
    global _last_untrusted_telemetry_at
    with _telemetry_lock:
        _last_untrusted_telemetry_at = 0.0


def _emit_untrusted_forwarding_telemetry(path: str) -> None:
    global _last_untrusted_telemetry_at
    now = time.monotonic()
    with _telemetry_lock:
        if now - _last_untrusted_telemetry_at < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
            return
        _last_untrusted_telemetry_at = now
    _logger.info(
        "Admin login source resolution rejected forwarding headers",
        extra={"source_resolution_path": path},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate ASGI peer is a member
    of ``settings.admin_trusted_proxy_networks``. The left-most ``X-Forwarded-For``
    value is never trusted directly; the parser walks the chain from the trusted
    edge toward the client (Cloudflare append semantics).
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    peer = _peer_host(request)
    peer_normalized = normalize_client_source(peer)

    if not trusted_networks:
        if peer_normalized is not None:
            return ClientSourceResolution(peer_normalized, "direct_peer")
        if peer:
            return ClientSourceResolution(peer.lower(), "direct_peer")
        return ClientSourceResolution("unknown", "unknown_peer")

    if peer_normalized is None or not _address_in_trusted_networks(
        peer_normalized, trusted_networks
    ):
        had_forwarding = any(
            request.headers.get(name, "").strip()
            for name in ("forwarded", "x-forwarded-for", "cf-connecting-ip")
        )
        if had_forwarding:
            _emit_untrusted_forwarding_telemetry("untrusted_peer")
        if peer_normalized is not None:
            return ClientSourceResolution(peer_normalized, "untrusted_peer")
        if peer:
            return ClientSourceResolution(peer.lower(), "untrusted_peer")
        return ClientSourceResolution("unknown", "unknown_peer")

    client, path = _resolve_from_forwarded_headers(request, trusted_networks)
    if client is not None:
        return ClientSourceResolution(client, path)

    if path == "malformed_forwarded":
        _emit_untrusted_forwarding_telemetry(path)
        return ClientSourceResolution(peer_normalized, "trusted_peer_fallback")

    if peer_normalized is not None:
        return ClientSourceResolution(peer_normalized, "trusted_peer_fallback")
    return ClientSourceResolution("unknown", "unknown_peer")
