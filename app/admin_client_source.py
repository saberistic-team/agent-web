"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

_MAX_FORWARDED_CHAIN_LENGTH = 32
_MAX_HEADER_LENGTH = 2048
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0

_telemetry_lock = threading.Lock()
_last_invalid_telemetry_at = 0.0


class SourceResolutionPath(StrEnum):
    """Bounded labels for operational telemetry (no raw addresses)."""

    DIRECT_PEER = "direct_peer"
    UNKNOWN = "unknown"
    CF_CONNECTING_IP = "cf_connecting_ip"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED = "forwarded"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved client source identity for limiter keying."""

    address: str
    path: SourceResolutionPath


def parse_trusted_proxy_networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured trusted-proxy CIDR strings into network objects."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        try:
            if "/" in value:
                networks.append(ipaddress.ip_network(value, strict=False))
            else:
                parsed = ipaddress.ip_address(value)
                prefix = 32 if parsed.version == 4 else 128
                networks.append(ipaddress.ip_network(f"{parsed}/{prefix}", strict=False))
        except ValueError:
            _logger.warning(
                "Ignoring invalid admin trusted proxy CIDR",
                extra={"cidr_token_length": len(value)},
            )
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Return a deterministic normalized IP string or ``None`` when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host_part, _, port_part = candidate[1:].partition("]")
        if port_part.startswith(":"):
            candidate = host_part
        else:
            candidate = host_part

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.rpartition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if parsed.version == 6:
        return parsed.compressed
    return str(parsed)


def is_trusted_proxy_address(address: str, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    """Return whether ``address`` falls within configured trusted-proxy networks."""
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarded_for_chain(header_value: str) -> list[str]:
    if len(header_value) > _MAX_HEADER_LENGTH:
        return []
    parts = [part.strip() for part in header_value.split(",")]
    if len(parts) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return parts


def resolve_from_x_forwarded_for(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk ``X-Forwarded-For`` right-to-left and return the first untrusted hop."""
    chain = _split_forwarded_for_chain(header_value)
    if not chain:
        return None

    normalized_chain: list[str] = []
    for hop in reversed(chain):
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)
        if not is_trusted_proxy_address(normalized, trusted_networks):
            return normalized

    if normalized_chain:
        return normalized_chain[-1]
    return None


_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|\"([^\"]+)\"|([^;,\s]+))',
    re.IGNORECASE,
)


def resolve_from_forwarded_header(header_value: str) -> str | None:
    """Extract the left-most ``for=`` identifier from an RFC 7239 ``Forwarded`` header."""
    if len(header_value) > _MAX_HEADER_LENGTH:
        return None
    for segment in header_value.split(","):
        match = _FORWARDED_FOR_RE.search(segment)
        if match is None:
            continue
        raw = next(group for group in match.groups() if group is not None)
        normalized = normalize_ip_address(raw)
        if normalized is not None:
            return normalized
    return None


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    return host or None


def _maybe_log_invalid_forwarding(*, had_forwarding_headers: bool, path: SourceResolutionPath) -> None:
    if not had_forwarding_headers or path is not SourceResolutionPath.INVALID_FORWARDING:
        return
    global _last_invalid_telemetry_at
    now = time.monotonic()
    with _telemetry_lock:
        if now - _last_invalid_telemetry_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
            return
        _last_invalid_telemetry_at = now
    _logger.info(
        "Admin login source forwarding headers ignored",
        extra={"source_resolution_path": SourceResolutionPath.INVALID_FORWARDING.value},
    )


def log_source_resolution(path: SourceResolutionPath) -> None:
    """Emit bounded telemetry for which source-resolution path was used."""
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_resolution_path": path.value},
    )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResult:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``settings.admin_trusted_proxy_cidrs``. Untrusted peers always resolve to
    the direct peer address so clients cannot spoof limiter buckets.
    """
    trusted_networks = parse_trusted_proxy_networks(settings.admin_trusted_proxy_cidrs)
    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResult(address="unknown", path=SourceResolutionPath.UNKNOWN)

    normalized_peer = normalize_ip_address(peer)
    cf_header = request.headers.get("cf-connecting-ip", "")
    xff_header = request.headers.get("x-forwarded-for", "")
    forwarded_header = request.headers.get("forwarded", "")
    had_forwarding_headers = bool(cf_header or xff_header or forwarded_header)

    if normalized_peer is None:
        if had_forwarding_headers:
            _maybe_log_invalid_forwarding(
                had_forwarding_headers=True,
                path=SourceResolutionPath.INVALID_FORWARDING,
            )
        fallback = peer.strip().lower() or "unknown"
        log_source_resolution(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResult(address=fallback, path=SourceResolutionPath.DIRECT_PEER)

    if trusted_networks and is_trusted_proxy_address(normalized_peer, trusted_networks):
        cf_candidate = normalize_ip_address(cf_header) if cf_header else None
        if cf_candidate is not None:
            log_source_resolution(SourceResolutionPath.CF_CONNECTING_IP)
            return ClientSourceResult(
                address=cf_candidate,
                path=SourceResolutionPath.CF_CONNECTING_IP,
            )

        if xff_header:
            xff_candidate = resolve_from_x_forwarded_for(
                xff_header,
                trusted_networks=trusted_networks,
            )
            if xff_candidate is not None:
                log_source_resolution(SourceResolutionPath.X_FORWARDED_FOR)
                return ClientSourceResult(
                    address=xff_candidate,
                    path=SourceResolutionPath.X_FORWARDED_FOR,
                )

        if forwarded_header:
            forwarded_candidate = resolve_from_forwarded_header(forwarded_header)
            if forwarded_candidate is not None:
                log_source_resolution(SourceResolutionPath.FORWARDED)
                return ClientSourceResult(
                    address=forwarded_candidate,
                    path=SourceResolutionPath.FORWARDED,
                )

        if had_forwarding_headers:
            _maybe_log_invalid_forwarding(
                had_forwarding_headers=True,
                path=SourceResolutionPath.INVALID_FORWARDING,
            )
            return ClientSourceResult(
                address="unknown",
                path=SourceResolutionPath.INVALID_FORWARDING,
            )

        log_source_resolution(SourceResolutionPath.UNKNOWN)
        return ClientSourceResult(address="unknown", path=SourceResolutionPath.UNKNOWN)

    if had_forwarding_headers:
        _maybe_log_invalid_forwarding(
            had_forwarding_headers=True,
            path=SourceResolutionPath.INVALID_FORWARDING,
        )

    log_source_resolution(SourceResolutionPath.DIRECT_PEER)
    return ClientSourceResult(address=normalized_peer, path=SourceResolutionPath.DIRECT_PEER)
