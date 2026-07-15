"""Secure client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 20
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_FORWARDED_HEADER_RE = re.compile(
    r'for=(?:"\[?([^;\]"]+)\]?"|([^;,\s]+))',
    re.IGNORECASE,
)

_last_telemetry_at: dict[str, float] = {}


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels for admin login source resolution."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    CF_CONNECTING_IP = "cf_connecting_ip"
    TRUSTED_FORWARDED = "trusted_forwarded"
    INVALID_FORWARDING = "invalid_forwarding"
    UNTRUSTED_SPOOF_ATTEMPT = "untrusted_spoof_attempt"


@dataclass(frozen=True)
class _TrustedBoundary:
    always_trust: bool
    trusted_literals: frozenset[str]
    trusted_hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @staticmethod
    def from_spec(spec: str) -> _TrustedBoundary:
        raw = spec.strip()
        if not raw:
            return _TrustedBoundary(False, frozenset(), frozenset(), ())
        if raw == "*":
            return _TrustedBoundary(True, frozenset(), frozenset(), ())

        literals: set[str] = set()
        hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in _split_csv_spec(raw):
            if "/" in item:
                try:
                    networks.append(ipaddress.ip_network(item, strict=False))
                except ValueError:
                    literals.add(item)
            else:
                try:
                    hosts.add(ipaddress.ip_address(item))
                except ValueError:
                    literals.add(item)
        return _TrustedBoundary(False, frozenset(literals), frozenset(hosts), tuple(networks))

    @functools.lru_cache(maxsize=4096)
    def contains(self, host: str) -> bool:
        if self.always_trust:
            return True
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip in self.trusted_hosts:
                return True
            return any(ip in network for network in self.trusted_networks)
        except ValueError:
            return host in self.trusted_literals


def _split_csv_spec(spec: str) -> list[str]:
    return [item.strip() for item in spec.split(",") if item.strip()]


def _parse_host_port(value: str) -> tuple[str, int]:
    """Parse a forwarded host value into host and optional port."""
    candidate = value.strip()
    if not candidate:
        return "", 0

    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            return candidate, 0
        host = candidate[1:bracket_end]
        remainder = candidate[bracket_end + 1 :]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return candidate, 0
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0

    if candidate.count(":") == 1:
        host, port_text = candidate.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return candidate, 0

    return candidate, 0


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    host, _port = _parse_host_port(raw.strip())
    if not host:
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


def _parse_forwarded_chain(raw: str) -> list[str]:
    if not raw.strip():
        return []
    hops: list[str] = []
    for entry in raw.split(","):
        match = _FORWARDED_HEADER_RE.search(entry)
        if match is None:
            continue
        candidate = match.group(1) or match.group(2) or ""
        host, _port = _parse_host_port(candidate)
        if host:
            hops.append(host)
    return hops


def _client_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _trusted_hop_boundary(settings: Settings) -> _TrustedBoundary:
    parts = [
        settings.admin_trusted_proxy_cidrs,
        settings.admin_cloudflare_trusted_cidrs,
    ]
    return _TrustedBoundary.from_spec(",".join(part for part in parts if part.strip()))


def _resolve_from_forwarded_chain(
    hops: list[str],
    *,
    trusted_boundary: _TrustedBoundary,
) -> str | None:
    if not hops:
        return None
    if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None

    if trusted_boundary.always_trust:
        return normalize_ip_address(hops[0])

    for hop in reversed(hops):
        host, _port = _parse_host_port(hop)
        if not host:
            continue
        if not trusted_boundary.contains(host):
            return normalize_ip_address(host)
    return normalize_ip_address(hops[0])


def _cloudflare_edge_present(
    hops: Iterable[str],
    *,
    cloudflare_boundary: _TrustedBoundary,
) -> bool:
    if not cloudflare_boundary.trusted_hosts and not cloudflare_boundary.trusted_networks:
        return False
    for hop in hops:
        host, _port = _parse_host_port(hop)
        if host and cloudflare_boundary.contains(host):
            return True
    return False


def _record_source_resolution(path: SourceResolutionPath, *, spoofed: bool = False) -> None:
    now = time.monotonic()
    key = path.value
    last = _last_telemetry_at.get(key)
    if last is not None and now - last < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
        return
    _last_telemetry_at[key] = now
    extra: dict[str, object] = {"source_resolution_path": key}
    if spoofed:
        extra["untrusted_forwarding_attempt"] = True
    _logger.info("Admin login client source resolved", extra=extra)


def reset_source_resolution_telemetry() -> None:
    """Clear sampled telemetry timestamps (tests only)."""
    _last_telemetry_at.clear()


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. Parsed chains walk trusted hops from right to
    left (Render / Cloudflare append order). ``CF-Connecting-IP`` is accepted
    only when a Cloudflare edge hop is present in the forwarding chain.
    """
    peer = _client_peer_host(request)
    normalized_peer = normalize_ip_address(peer) if peer is not None else None
    trusted_proxy_boundary = _TrustedBoundary.from_spec(settings.admin_trusted_proxy_cidrs)
    trusted_hops = _trusted_hop_boundary(settings)
    cloudflare_boundary = _TrustedBoundary.from_spec(settings.admin_cloudflare_trusted_cidrs)

    forwarding_headers_present = any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )

    if peer is None:
        if forwarding_headers_present:
            _record_source_resolution(
                SourceResolutionPath.INVALID_FORWARDING,
                spoofed=True,
            )
        _record_source_resolution(SourceResolutionPath.DIRECT_PEER)
        return "unknown"

    if not trusted_proxy_boundary.contains(peer):
        if forwarding_headers_present:
            _record_source_resolution(
                SourceResolutionPath.UNTRUSTED_SPOOF_ATTEMPT,
                spoofed=True,
            )
        _record_source_resolution(SourceResolutionPath.DIRECT_PEER)
        return normalized_peer if normalized_peer is not None else peer

    fallback_peer = normalized_peer if normalized_peer is not None else peer

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_hops = [hop for hop in (_parse_host_port(item)[0] for item in xff_raw.split(",")) if hop]
    forwarded_raw = request.headers.get("forwarded", "")
    forwarded_hops = _parse_forwarded_chain(forwarded_raw)
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")

    if cf_connecting_ip.strip():
        if _cloudflare_edge_present(xff_hops, cloudflare_boundary=cloudflare_boundary):
            normalized_cf = normalize_ip_address(cf_connecting_ip)
            if normalized_cf is not None:
                _record_source_resolution(SourceResolutionPath.CF_CONNECTING_IP)
                return normalized_cf
        _record_source_resolution(
            SourceResolutionPath.UNTRUSTED_SPOOF_ATTEMPT,
            spoofed=True,
        )

    if xff_hops:
        resolved = _resolve_from_forwarded_chain(xff_hops, trusted_boundary=trusted_hops)
        if resolved is not None:
            _record_source_resolution(SourceResolutionPath.TRUSTED_XFF)
            return resolved
        _record_source_resolution(SourceResolutionPath.INVALID_FORWARDING, spoofed=True)
        return fallback_peer

    if forwarded_hops:
        resolved = _resolve_from_forwarded_chain(forwarded_hops, trusted_boundary=trusted_hops)
        if resolved is not None:
            _record_source_resolution(SourceResolutionPath.TRUSTED_FORWARDED)
            return resolved
        _record_source_resolution(SourceResolutionPath.INVALID_FORWARDING, spoofed=True)
        return fallback_peer

    _record_source_resolution(SourceResolutionPath.DIRECT_PEER)
    return fallback_peer
