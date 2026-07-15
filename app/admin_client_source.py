"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

SOURCE_UNKNOWN = "unknown"
MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDING_LOG_SAMPLE_RATE = 100

# Render/platform private ranges used as the immediate-peer trust boundary.
_DEFAULT_PLATFORM_TRUSTED_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/) for
# right-to-left stripping after the Render load balancer hop.
_CLOUDFLARE_TRUSTED_CIDRS: tuple[str, ...] = (
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "108.162.192.0/18",
    "131.0.72.0/22",
    "141.101.64.0/18",
    "162.158.0.0/15",
    "172.64.0.0/13",
    "173.245.48.0/20",
    "188.114.96.0/20",
    "190.93.240.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_FORWARDED_FOR_SPLIT = re.compile(r",")
_FORWARDED_FOR_TOKEN = re.compile(
    r"for=(?:\"(?P<quoted>[^\"]+)\"|(?P<unquoted>[^;,\s]+))",
    re.IGNORECASE,
)

_telemetry_lock = threading.Lock()
_invalid_forwarding_log_counter = 0


class SourceResolutionPath(StrEnum):
    """Bounded telemetry labels — no raw addresses or header values."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_TRUSTED_CHAIN = "forwarded_trusted_chain"
    CF_CONNECTING_IP_TRUSTED_EDGE = "cf_connecting_ip_trusted_edge"
    MALFORMED_FORWARDING = "malformed_forwarding"
    OVERLONG_CHAIN = "overlong_chain"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: SourceResolutionPath


def reset_source_resolution_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _invalid_forwarding_log_counter
    with _telemetry_lock:
        _invalid_forwarding_log_counter = 0


def _parse_network_specs(specs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for spec in specs:
        trimmed = spec.strip()
        if not trimmed:
            continue
        try:
            networks.append(ipaddress.ip_network(trimmed, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return the configured trusted-proxy boundary for admin login source resolution."""
    specs: list[str] = list(_DEFAULT_PLATFORM_TRUSTED_CIDRS)
    if settings.admin_trust_cloudflare_proxies:
        specs.extend(_CLOUDFLARE_TRUSTED_CIDRS)
    specs.extend(settings.admin_trusted_proxy_ips)
    return _parse_network_specs(specs)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 deterministically; return None when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing != -1:
            candidate = candidate[1:closing]
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
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _address_in_trusted_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address in network for network in networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() or None


def _split_forwarded_for(header_value: str) -> list[str]:
    return [part.strip() for part in _FORWARDED_FOR_SPLIT.split(header_value) if part.strip()]


def _parse_forwarded_header(header_value: str) -> list[str]:
    addresses: list[str] = []
    for match in _FORWARDED_FOR_TOKEN.finditer(header_value):
        raw = match.group("quoted") or match.group("unquoted")
        normalized = normalize_client_address(raw)
        if normalized is not None:
            addresses.append(normalized)
    return addresses


def _resolve_from_trusted_chain(
    chain: list[str],
    *,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain:
        return None
    if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
        return None

    parsed_chain: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        parsed_chain.append(ipaddress.ip_address(normalized))

    for index in range(len(parsed_chain) - 1, -1, -1):
        hop = parsed_chain[index]
        if _address_in_trusted_networks(hop, networks):
            continue
        return normalize_client_address(str(hop))

    if parsed_chain:
        return normalize_client_address(str(parsed_chain[0]))
    return None


def _peer_is_trusted(
    peer_host: str | None,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if peer_host is None:
        return False
    normalized = normalize_client_address(peer_host)
    if normalized is None:
        return False
    peer = ipaddress.ip_address(normalized)
    return _address_in_trusted_networks(peer, networks)


def _log_resolution_telemetry(path: SourceResolutionPath, *, sampled_invalid: bool = False) -> None:
    global _invalid_forwarding_log_counter
    if sampled_invalid:
        with _telemetry_lock:
            _invalid_forwarding_log_counter += 1
            should_log = _invalid_forwarding_log_counter % _INVALID_FORWARDING_LOG_SAMPLE_RATE == 1
        if not should_log:
            return
        extra = {"resolution_path": path.value, "sampled": True}
    else:
        extra = {"resolution_path": path.value}
    _logger.info("Admin login client source resolved", extra=extra)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate TCP peer is a member of
    the configured trusted-proxy boundary. Header chains are parsed right-to-left,
    stripping trusted hops, so an attacker-controlled leftmost ``X-Forwarded-For``
    value cannot mint a fresh limiter bucket.
    """
    peer_host = _immediate_peer_host(request)
    if peer_host is None:
        resolution = ClientSourceResolution(SOURCE_UNKNOWN, SourceResolutionPath.MISSING_PEER)
        _log_resolution_telemetry(resolution.path)
        return resolution

    peer_source = normalize_client_address(peer_host) or peer_host.strip()
    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(peer_source, SourceResolutionPath.DIRECT_PEER)
        _log_resolution_telemetry(resolution.path)
        return resolution

    networks = trusted_proxy_networks(settings)
    if not _peer_is_trusted(peer_host, networks):
        resolution = ClientSourceResolution(peer_source, SourceResolutionPath.UNTRUSTED_PEER)
        _log_resolution_telemetry(resolution.path, sampled_invalid=True)
        return resolution

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        chain = _split_forwarded_for(xff_header)
        if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
            resolution = ClientSourceResolution(peer_source, SourceResolutionPath.OVERLONG_CHAIN)
            _log_resolution_telemetry(resolution.path, sampled_invalid=True)
            return resolution
        resolved = _resolve_from_trusted_chain(chain, networks=networks)
        if resolved is not None:
            resolution = ClientSourceResolution(resolved, SourceResolutionPath.XFF_TRUSTED_CHAIN)
            _log_resolution_telemetry(resolution.path)
            return resolution
        resolution = ClientSourceResolution(peer_source, SourceResolutionPath.MALFORMED_FORWARDING)
        _log_resolution_telemetry(resolution.path, sampled_invalid=True)
        return resolution

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        chain = _parse_forwarded_header(forwarded_header)
        if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
            resolution = ClientSourceResolution(peer_source, SourceResolutionPath.OVERLONG_CHAIN)
            _log_resolution_telemetry(resolution.path, sampled_invalid=True)
            return resolution
        resolved = _resolve_from_trusted_chain(chain, networks=networks)
        if resolved is not None:
            resolution = ClientSourceResolution(
                resolved,
                SourceResolutionPath.FORWARDED_TRUSTED_CHAIN,
            )
            _log_resolution_telemetry(resolution.path)
            return resolution
        resolution = ClientSourceResolution(peer_source, SourceResolutionPath.MALFORMED_FORWARDING)
        _log_resolution_telemetry(resolution.path, sampled_invalid=True)
        return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        # Vendor header accepted only after the immediate peer is a trusted edge hop.
        normalized = normalize_client_address(cf_header)
        if normalized is not None:
            resolution = ClientSourceResolution(
                normalized,
                SourceResolutionPath.CF_CONNECTING_IP_TRUSTED_EDGE,
            )
            _log_resolution_telemetry(resolution.path)
            return resolution
        resolution = ClientSourceResolution(peer_source, SourceResolutionPath.MALFORMED_FORWARDING)
        _log_resolution_telemetry(resolution.path, sampled_invalid=True)
        return resolution

    resolution = ClientSourceResolution(peer_source, SourceResolutionPath.DIRECT_PEER)
    _log_resolution_telemetry(resolution.path)
    return resolution
