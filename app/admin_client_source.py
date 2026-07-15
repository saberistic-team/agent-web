"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Conservative cap on forwarded-hop parsing depth.
_MAX_FORWARDED_HOPS = 20

# Sample at most one invalid/untrusted forwarding telemetry event per interval.
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_last_untrusted_forwarding_log_at = 0.0

_logger = logging.getLogger(__name__)

# Default trusted proxy boundary for Render's internal load balancer (RFC1918 + loopback).
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

# RFC 7239 Forwarded: for=...;proto=... (quoted or unquoted for= value).
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[?([^"\];]+)\]?"|([^;\s]+))',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a privacy-safe telemetry path label."""

    address: str
    path: str


def default_trusted_proxy_cidrs() -> tuple[str, ...]:
    """Return the built-in trusted-proxy CIDR list used when none is configured."""
    return _DEFAULT_TRUSTED_PROXY_CIDRS


def parse_trusted_proxy_cidrs(raw: str | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted-proxy CIDRs; ignore malformed entries."""
    if not raw or not raw.strip():
        return tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in _DEFAULT_TRUSTED_PROXY_CIDRS
        )

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    if not networks:
        return tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in _DEFAULT_TRUSTED_PROXY_CIDRS
        )
    return tuple(networks)


def _strip_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end].strip()
        return value
    if value.count(":") == 1 and "." in value:
        host, _port = value.rsplit(":", 1)
        if _port.isdigit():
            return host.strip()
    return value


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 for deterministic limiter keys; return None when invalid."""
    candidate = _strip_port(raw.strip().strip('"').strip("'"))
    if not candidate or candidate.lower() == "unknown":
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return str(parsed)


def is_trusted_proxy_address(
    address: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return whether ``address`` falls inside the configured trusted-proxy boundary."""
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = (request.client.host or "").strip()
    return host or None


def _split_forwarding_chain(raw: str) -> list[str]:
    elements: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if candidate:
            elements.append(candidate)
    return elements


def _walk_trusted_xff_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Select the rightmost hop that is not a trusted proxy (walk right-to-left)."""
    if len(hops) > _MAX_FORWARDED_HOPS:
        return None

    normalized_hops: list[str] = []
    for hop in reversed(hops):
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    for hop in normalized_hops:
        if not is_trusted_proxy_address(hop, trusted_networks):
            return hop
    return None


def _resolve_from_x_forwarded_for(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    hops = _split_forwarding_chain(header_value)
    if not hops:
        return None
    return _walk_trusted_xff_chain(hops, trusted_networks=trusted_networks)


def _resolve_from_forwarded_header(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    hops: list[str] = []
    for segment in header_value.split(","):
        match = _FORWARDED_FOR_RE.search(segment)
        if match is None:
            continue
        hop = match.group(1) or match.group(2) or ""
        hop = hop.strip()
        if hop.lower().startswith("unknown"):
            continue
        if hop:
            hops.append(hop)
    if not hops:
        return None
    return _walk_trusted_xff_chain(hops, trusted_networks=trusted_networks)


def _has_untrusted_forwarding_headers(request: Request) -> bool:
    for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip"):
        if request.headers.get(name):
            return True
    return False


def _maybe_log_untrusted_forwarding(*, path: str, had_forwarding_headers: bool) -> None:
    global _last_untrusted_forwarding_log_at

    if not had_forwarding_headers and path != "invalid_forwarding":
        return

    now = time.monotonic()
    if now - _last_untrusted_forwarding_log_at < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
        return
    _last_untrusted_forwarding_log_at = now
    _logger.info(
        "Admin login client source used conservative path",
        extra={
            "client_source_path": path,
            "untrusted_forwarding_observed": had_forwarding_headers,
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Internet client → Cloudflare edge → Render load balancer → Uvicorn``

    Forwarding headers are honored only when the immediate TCP peer is inside the
    configured trusted-proxy boundary. The leftmost ``X-Forwarded-For`` value is
    never trusted directly; hops are evaluated from right to left, skipping trusted
    proxies until the first untrusted address is found.

    Header precedence when the immediate peer is trusted:

    1. ``X-Forwarded-For`` (right-to-left trusted-hop walk)
    2. ``Forwarded`` (RFC 7239 ``for=`` values, same walk)
    3. ``CF-Connecting-IP`` (only after peer verification; ignored for direct peers)

    Raw addresses and header chains are never logged.
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    peer = _immediate_peer_host(request)
    had_forwarding_headers = _has_untrusted_forwarding_headers(request)

    if peer is None:
        resolution = ClientSourceResolution(address="unknown", path="missing_peer")
        _maybe_log_untrusted_forwarding(
            path=resolution.path,
            had_forwarding_headers=had_forwarding_headers,
        )
        return resolution

    peer_normalized = normalize_ip_address(peer)
    if peer_normalized is None:
        if not settings.admin_trust_proxy_headers:
            resolution = ClientSourceResolution(
                address=peer.strip().lower(),
                path="direct_peer",
            )
        else:
            resolution = ClientSourceResolution(address="unknown", path="invalid_peer")
        _maybe_log_untrusted_forwarding(
            path=resolution.path,
            had_forwarding_headers=had_forwarding_headers,
        )
        return resolution

    if not settings.admin_trust_proxy_headers or not is_trusted_proxy_address(
        peer_normalized, trusted_networks
    ):
        resolution = ClientSourceResolution(address=peer_normalized, path="direct_peer")
        _maybe_log_untrusted_forwarding(
            path=resolution.path,
            had_forwarding_headers=had_forwarding_headers,
        )
        return resolution

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        resolved = _resolve_from_x_forwarded_for(xff, trusted_networks=trusted_networks)
        if resolved is not None:
            return ClientSourceResolution(address=resolved, path="trusted_x_forwarded_for")

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        resolved = _resolve_from_forwarded_header(forwarded, trusted_networks=trusted_networks)
        if resolved is not None:
            return ClientSourceResolution(address=resolved, path="trusted_forwarded")

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip:
        resolved = normalize_ip_address(cf_connecting_ip)
        if resolved is not None:
            return ClientSourceResolution(address=resolved, path="trusted_cf_connecting_ip")

    resolution = ClientSourceResolution(address="unknown", path="invalid_forwarding")
    _maybe_log_untrusted_forwarding(
        path=resolution.path,
        had_forwarding_headers=had_forwarding_headers,
    )
    return resolution


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry state (tests only)."""
    global _last_untrusted_forwarding_log_at
    _last_untrusted_forwarding_log_at = 0.0
