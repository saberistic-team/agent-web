"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum

from fastapi import Request

from app.config import DEFAULT_TRUSTED_PROXY_CIDRS, Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0
_last_invalid_telemetry_at = 0.0

_FORWARDED_FOR_SPLIT = re.compile(r"\s*,\s*")
_FORWARDED_ENTRY_SPLIT = re.compile(r"\s*,\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")
_FORWARDED_FOR_PARAM = re.compile(
    r'for=(?:"\[([^\]]+)\]"|\"([^\"]+)\"|([^;,\s]+))',
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    PEER_DIRECT = "peer_direct"
    UNTRUSTED_PEER = "untrusted_peer"
    FORWARDED_RTL = "forwarded_rtl"
    FORWARDED_RFC7239 = "forwarded_rfc7239"
    CF_CONNECTING_VERIFIED = "cf_connecting_verified"
    MISSING_OR_MALFORMED = "missing_or_malformed"
    UNKNOWN_PEER = "unknown_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    address: str
    path: SourceResolutionPath


class _TrustedProxyBoundary:
    """Membership checks for configured trusted proxy hosts and networks."""

    def __init__(self, entries: tuple[str, ...]) -> None:
        self.always_trust = entries == ("*",)
        self.literals: set[str] = set()
        self.hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
        if self.always_trust:
            return
        for entry in entries:
            if "/" in entry:
                try:
                    self.networks.add(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    self.literals.add(entry)
            else:
                try:
                    self.hosts.add(ipaddress.ip_address(entry))
                except ValueError:
                    self.literals.add(entry)
        self._trusts = functools.lru_cache(maxsize=4096)(self._compute_trust)

    def __contains__(self, host: str | None) -> bool:
        if self.always_trust:
            return True
        if not host:
            return False
        if len(host) > 253:
            return self._compute_trust(host)
        return self._trusts(host)

    def _compute_trust(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host in self.literals
        if ip in self.hosts:
            return True
        return any(ip in network for network in self.networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize one client/proxy hop to a canonical IP string, or None if invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > 253:
        return None

    host = candidate
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder:
            if not remainder.startswith(":"):
                return None
            try:
                int(remainder[1:])
            except ValueError:
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        try:
            int(port)
        except ValueError:
            return None

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv4Address):
        return str(ip)
    return ip.compressed


def _parse_host_port(value: str) -> tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return value, 0
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return value, 0
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0
    if value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0
    return value, 0


def _right_to_left_untrusted_hop(
    hops: list[str],
    trusted: _TrustedProxyBoundary,
) -> str | None:
    if len(hops) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    if trusted.always_trust:
        if not hops:
            return None
        first = normalize_client_address(hops[0])
        return first
    for hop in reversed(hops):
        host, _port = _parse_host_port(hop.strip())
        normalized = normalize_client_address(host)
        if normalized is None:
            return None
        if normalized not in trusted:
            return normalized
    if not hops:
        return None
    host, _port = _parse_host_port(hops[0].strip())
    return normalize_client_address(host)


def _parse_x_forwarded_for(header_value: str) -> list[str]:
    return [part for part in _FORWARDED_FOR_SPLIT.split(header_value) if part != ""]


def _parse_forwarded_header(header_value: str) -> list[str]:
    entries: list[str] = []
    for entry in _FORWARDED_ENTRY_SPLIT.split(header_value):
        match = _FORWARDED_FOR_PARAM.search(entry)
        if match is None:
            continue
        value = match.group(1) or match.group(2) or match.group(3) or ""
        if value:
            entries.append(value)
    return entries


def _cloudflare_hop_verified(
    x_forwarded_for: str,
    cloudflare_boundary: _TrustedProxyBoundary,
) -> bool:
    if cloudflare_boundary.always_trust:
        return False
    for hop in _parse_x_forwarded_for(x_forwarded_for):
        host, _port = _parse_host_port(hop.strip())
        normalized = normalize_client_address(host)
        if normalized is None:
            continue
        if normalized in cloudflare_boundary:
            return True
    return False


def _maybe_log_invalid_forwarding(path: SourceResolutionPath, reason: str) -> None:
    global _last_invalid_telemetry_at
    now = time.monotonic()
    if now - _last_invalid_telemetry_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
        return
    _last_invalid_telemetry_at = now
    _logger.info(
        "Admin login source forwarding rejected",
        extra={
            "source_resolution_path": path.value,
            "forwarding_rejection_reason": reason,
        },
    )


def reset_source_resolution_telemetry() -> None:
    """Clear sampled invalid-forwarding telemetry state (tests only)."""
    global _last_invalid_telemetry_at
    _last_invalid_telemetry_at = 0.0


def _trusted_boundary(settings: Settings) -> _TrustedProxyBoundary:
    return _TrustedProxyBoundary(settings.admin_trusted_proxy_ips)


def _cloudflare_boundary(settings: Settings) -> _TrustedProxyBoundary:
    return _TrustedProxyBoundary(settings.admin_cloudflare_proxy_cidrs)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented): browser → Cloudflare edge → Render load
    balancer → Uvicorn (``ProxyHeadersMiddleware`` with explicit
    ``--forwarded-allow-ips``) → application resolver.

    Forwarding headers are read only when the immediate peer is a member of the
    configured trusted-proxy boundary. Client identity is taken from the
    rightmost untrusted hop in ``X-Forwarded-For`` / ``Forwarded`` (matching
    Uvicorn's trusted-hop semantics). ``CF-Connecting-IP`` is accepted only
    when a Cloudflare hop is present in the verified forwarding chain so direct
    Render-origin requests cannot spoof edge-derived addresses.
    """
    if request.client is None:
        _maybe_log_invalid_forwarding(
            SourceResolutionPath.UNKNOWN_PEER,
            "missing_peer",
        )
        return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)

    peer_host, _peer_port = request.client
    peer_literal = peer_host.strip().lower()
    peer_normalized = normalize_client_address(peer_host)
    peer_source = peer_normalized if peer_normalized is not None else peer_literal
    trusted_peer_key = peer_normalized if peer_normalized is not None else peer_literal

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(peer_source, SourceResolutionPath.PEER_DIRECT)

    trusted = _trusted_boundary(settings)
    if trusted_peer_key not in trusted:
        return ClientSourceResolution(peer_source, SourceResolutionPath.UNTRUSTED_PEER)

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    if x_forwarded_for:
        hops = _parse_x_forwarded_for(x_forwarded_for)
        resolved = _right_to_left_untrusted_hop(hops, trusted)
        if resolved is not None and resolved not in trusted:
            return ClientSourceResolution(resolved, SourceResolutionPath.FORWARDED_RTL)
        if resolved is None:
            _maybe_log_invalid_forwarding(
                SourceResolutionPath.MISSING_OR_MALFORMED,
                "invalid_x_forwarded_for",
            )

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        hops = _parse_forwarded_header(forwarded)
        resolved = _right_to_left_untrusted_hop(hops, trusted)
        if resolved is not None and resolved not in trusted:
            return ClientSourceResolution(resolved, SourceResolutionPath.FORWARDED_RFC7239)
        if resolved is None:
            _maybe_log_invalid_forwarding(
                SourceResolutionPath.MISSING_OR_MALFORMED,
                "invalid_forwarded",
            )

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip and x_forwarded_for:
        cloudflare = _cloudflare_boundary(settings)
        if not cloudflare.always_trust and _cloudflare_hop_verified(x_forwarded_for, cloudflare):
            resolved = normalize_client_address(cf_connecting_ip)
            if resolved is not None:
                return ClientSourceResolution(
                    resolved,
                    SourceResolutionPath.CF_CONNECTING_VERIFIED,
                )
        _maybe_log_invalid_forwarding(
            SourceResolutionPath.MISSING_OR_MALFORMED,
            "unverified_cf_connecting_ip",
        )

    _maybe_log_invalid_forwarding(
        SourceResolutionPath.MISSING_OR_MALFORMED,
        "no_trusted_forwarding_chain",
    )
    return ClientSourceResolution("unknown", SourceResolutionPath.MISSING_OR_MALFORMED)
