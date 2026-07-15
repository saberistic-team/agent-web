"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Cloudflare → Render → Uvicorn: cap forwarded chains to bound parser work.
MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDED_LOG_INTERVAL_SECONDS = 60.0
_last_invalid_forwarded_log_at = 0.0

_FORWARDED_FOR_PARAM_RE = re.compile(
    r"for=(?:\"(?P<quoted>[^\"]+)\"|\[(?P<bracket>[^\]]+)\]|(?P<bare>[^;,\s]+))",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    TRUSTED_X_FORWARDED_FOR = "trusted_x_forwarded_for"
    TRUSTED_FORWARDED_HEADER = "trusted_forwarded_header"
    INVALID_FORWARDED = "invalid_forwarded"
    UNKNOWN_PEER = "unknown_peer"
    CHAIN_TOO_LONG = "chain_too_long"


@dataclass(frozen=True)
class TrustedProxyBoundary:
    """Configured trusted immediate peers (Render load balancer / private hops)."""

    hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]
    literals: frozenset[str]

    @classmethod
    def from_csv(cls, raw: str) -> TrustedProxyBoundary:
        hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
        literals: set[str] = set()
        for item in raw.split(","):
            entry = item.strip()
            if not entry:
                continue
            if "/" in entry:
                try:
                    networks.add(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    literals.add(entry)
                continue
            try:
                hosts.add(ipaddress.ip_address(entry))
            except ValueError:
                literals.add(entry)
        return cls(
            hosts=frozenset(hosts),
            networks=frozenset(networks),
            literals=frozenset(literals),
        )

    @property
    def configured(self) -> bool:
        return bool(self.hosts or self.networks or self.literals)

    def trusts(self, host: str) -> bool:
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip in self.hosts:
                return True
            return any(ip in network for network in self.networks)
        except ValueError:
            return host in self.literals


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and a privacy-safe telemetry path."""

    source_material: str
    path: SourceResolutionPath


def parse_trusted_proxy_cidrs(raw: str) -> TrustedProxyBoundary:
    """Parse ``ADMIN_TRUSTED_PROXY_CIDRS`` (comma-separated IPs/CIDRs)."""
    return TrustedProxyBoundary.from_csv(raw)


def _parse_host_port(value: str) -> tuple[str, int | None]:
    candidate = value.strip()
    if not candidate:
        return "", None
    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            return candidate, None
        host = candidate[1:bracket_end]
        remainder = candidate[bracket_end + 1 :]
        if remainder.startswith(":"):
            try:
                return host, int(remainder[1:])
            except ValueError:
                return host, None
        return host, None
    if candidate.count(":") == 1 and "." in candidate:
        host, port_text = candidate.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return candidate, None
    return candidate, None


def normalize_client_address(value: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) for deterministic limiter keys."""
    host, _port = _parse_host_port(value)
    if not host or host.lower() == "unknown":
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.compressed
    return str(ip)


def _split_forwarded_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def resolve_from_x_forwarded_for(
    header_value: str,
    trusted: TrustedProxyBoundary,
) -> str | None:
    """Walk ``X-Forwarded-For`` right-to-left; return the first untrusted hop."""
    hops = _split_forwarded_chain(header_value)
    if not hops:
        return None
    if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    for hop in reversed(hops):
        host, _port = _parse_host_port(hop)
        if not host:
            continue
        if not trusted.trusts(host):
            return normalize_client_address(host)
    first_host, _port = _parse_host_port(hops[0])
    return normalize_client_address(first_host) if first_host else None


def resolve_from_forwarded_header(header_value: str) -> str | None:
    """Extract the left-most ``for=`` client from an RFC 7239 ``Forwarded`` header."""
    for entry in _split_forwarded_chain(header_value):
        match = _FORWARDED_FOR_PARAM_RE.search(entry)
        if match is None:
            continue
        raw = match.group("quoted") or match.group("bracket") or match.group("bare") or ""
        candidate = raw.strip()
        if not candidate or candidate.lower() == "unknown":
            continue
        if candidate.startswith("_"):
            continue
        normalized = normalize_client_address(candidate)
        if normalized is not None:
            return normalized
    return None


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _log_source_resolution(
    path: SourceResolutionPath,
    *,
    untrusted_forwarded: bool = False,
) -> None:
    global _last_invalid_forwarded_log_at
    extra = {
        "admin_login_source_path": path.value,
        "untrusted_forwarded": untrusted_forwarded,
    }
    if path in {
        SourceResolutionPath.INVALID_FORWARDED,
        SourceResolutionPath.UNTRUSTED_PEER,
        SourceResolutionPath.CHAIN_TOO_LONG,
    }:
        now = time.monotonic()
        if now - _last_invalid_forwarded_log_at < _INVALID_FORWARDED_LOG_INTERVAL_SECONDS:
            return
        _last_invalid_forwarded_log_at = now
        _logger.info("Admin login source resolution rejected forwarded identity", extra=extra)
        return
    _logger.debug("Admin login source resolution", extra=extra)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Client → Cloudflare (public edge) → Render load balancer → Uvicorn``.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_CIDRS``. The left-most ``X-Forwarded-For`` value is
    never trusted from arbitrary peers. When the peer is trusted, header
    precedence is:

    1. ``CF-Connecting-IP`` (Cloudflare edge; ignored on direct origin access)
    2. ``X-Forwarded-For`` parsed right-to-left against the trusted boundary
    3. ``Forwarded`` (RFC 7239 ``for=`` parameter)
    4. Immediate peer address
    """
    peer = _immediate_peer_host(request)
    if peer is None:
        _log_source_resolution(SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)

    peer_normalized = normalize_client_address(peer)
    fallback = peer_normalized if peer_normalized is not None else peer.strip().lower()
    if not fallback:
        fallback = "unknown"

    if not settings.admin_trust_proxy_headers:
        _log_source_resolution(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(fallback, SourceResolutionPath.DIRECT_PEER)

    trusted = parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    if not trusted.configured or not trusted.trusts(peer):
        has_forwarded = any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        )
        _log_source_resolution(
            SourceResolutionPath.UNTRUSTED_PEER,
            untrusted_forwarded=has_forwarded,
        )
        return ClientSourceResolution(fallback, SourceResolutionPath.UNTRUSTED_PEER)

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        normalized_cf = normalize_client_address(cf_connecting_ip)
        if normalized_cf is not None:
            _log_source_resolution(SourceResolutionPath.TRUSTED_CF_CONNECTING_IP)
            return ClientSourceResolution(
                normalized_cf,
                SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
            )

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        hops = _split_forwarded_chain(x_forwarded_for)
        if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
            _log_source_resolution(
                SourceResolutionPath.CHAIN_TOO_LONG,
                untrusted_forwarded=True,
            )
            return ClientSourceResolution(fallback, SourceResolutionPath.CHAIN_TOO_LONG)
        normalized_xff = resolve_from_x_forwarded_for(x_forwarded_for, trusted)
        if normalized_xff is not None:
            _log_source_resolution(SourceResolutionPath.TRUSTED_X_FORWARDED_FOR)
            return ClientSourceResolution(
                normalized_xff,
                SourceResolutionPath.TRUSTED_X_FORWARDED_FOR,
            )

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        normalized_forwarded = resolve_from_forwarded_header(forwarded)
        if normalized_forwarded is not None:
            _log_source_resolution(SourceResolutionPath.TRUSTED_FORWARDED_HEADER)
            return ClientSourceResolution(
                normalized_forwarded,
                SourceResolutionPath.TRUSTED_FORWARDED_HEADER,
            )

    if any((cf_connecting_ip, x_forwarded_for, forwarded)):
        _log_source_resolution(
            SourceResolutionPath.INVALID_FORWARDED,
            untrusted_forwarded=True,
        )
        return ClientSourceResolution(fallback, SourceResolutionPath.INVALID_FORWARDED)

    _log_source_resolution(SourceResolutionPath.DIRECT_PEER)
    return ClientSourceResolution(fallback, SourceResolutionPath.DIRECT_PEER)
