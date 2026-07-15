"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Cloudflare published edge ranges (https://www.cloudflare.com/ips-v4/ / ips-v6/).
# Used to skip verified edge hops when walking X-Forwarded-For right-to-left.
_CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
_CLOUDFLARE_IPV6_CIDRS: tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Render's web proxy connects over the platform internal network. The process is
# not reachable as these addresses from the public Internet; they identify the
# immediate peer that may append forwarding headers.
_RENDER_INTERNAL_IPV4_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_MAX_HEADER_LENGTH = 2048

_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0
_invalid_telemetry_last_logged = 0.0
_invalid_telemetry_suppressed = 0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source identity was resolved."""

    PEER_DIRECT = "peer_direct"
    X_FORWARDED_FOR = "x_forwarded_for"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED = "forwarded"
    PEER_FALLBACK = "peer_fallback"
    UNKNOWN = "unknown"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    address: str
    path: SourceResolutionPath


def default_trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Production defaults: Render internal peers plus Cloudflare edge hops."""
    return _parse_network_specs(
        _RENDER_INTERNAL_IPV4_CIDRS + _CLOUDFLARE_IPV4_CIDRS + _CLOUDFLARE_IPV6_CIDRS
    )


def parse_trusted_proxy_networks(specs: Iterable[str]) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Parse comma-separated CIDRs and single addresses from configuration."""
    return _parse_network_specs(specs)


def _parse_network_specs(
    specs: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_spec in specs:
        spec = raw_spec.strip()
        if not spec:
            continue
        try:
            if "/" in spec:
                networks.append(ipaddress.ip_network(spec, strict=False))
            else:
                address = ipaddress.ip_address(spec)
                networks.append(
                    ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(value: str | None) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 deterministically."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > _MAX_HEADER_LENGTH:
        return None

    host = candidate
    if candidate.startswith("[") and "]" in candidate:
        host = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        # IPv4 with port, e.g. 203.0.113.1:443
        host = candidate.rsplit(":", 1)[0]

    try:
        parsed = ipaddress.ip_address(host.strip())
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _opaque_peer_identifier(peer: str | None) -> str | None:
    if peer is None:
        return None
    candidate = peer.strip().lower()
    if not candidate or len(candidate) > _MAX_HEADER_LENGTH:
        return None
    return candidate


def _resolved_peer_address(peer: str | None) -> str:
    normalized = normalize_client_address(peer)
    if normalized is not None:
        return normalized
    return _opaque_peer_identifier(peer) or "unknown"


def is_trusted_proxy_address(
    address: str | None,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """Return whether ``address`` belongs to a configured trusted proxy network."""
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _cloudflare_edge_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse_network_specs(_CLOUDFLARE_IPV4_CIDRS + _CLOUDFLARE_IPV6_CIDRS)


def _cloudflare_edge_seen(
    hosts: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    edge_networks = _cloudflare_edge_networks()
    for host in hosts:
        normalized = normalize_client_address(host)
        if normalized is None:
            continue
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            continue
        if any(parsed in network for network in edge_networks):
            return True
        if any(parsed in network for network in trusted_networks):
            # A trusted hop that is not the immediate peer still proves a proxy chain.
            continue
    return False


def _parse_x_forwarded_for_hosts(header_value: str) -> list[str] | None:
    if len(header_value) > _MAX_HEADER_LENGTH:
        return None
    hosts = [segment.strip() for segment in header_value.split(",")]
    if not hosts or len(hosts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    if any(not segment for segment in hosts):
        return None
    return hosts


def _client_from_x_forwarded_for(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    hosts = _parse_x_forwarded_for_hosts(header_value)
    if hosts is None:
        return None

    for host_port in reversed(hosts):
        normalized = normalize_client_address(host_port)
        if normalized is None:
            return None
        if not is_trusted_proxy_address(normalized, trusted_networks):
            return normalized

    # Every hop is trusted; defer to vendor-specific headers or peer fallback.
    return None


def _parse_forwarded_header(header_value: str) -> str | None:
    if len(header_value) > _MAX_HEADER_LENGTH:
        return None
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        for part in entry.split(";"):
            part = part.strip()
            if part.lower().startswith("for="):
                value = part[4:].strip().strip('"')
                if value.lower() in {"unknown", "[unknown]"}:
                    continue
                normalized = normalize_client_address(value)
                if normalized is not None:
                    return normalized
    return None


def _trusted_networks_for_settings(settings: Settings) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    if settings.admin_trusted_proxy_ips:
        return parse_trusted_proxy_networks(
            segment.strip()
            for segment in settings.admin_trusted_proxy_ips.split(",")
            if segment.strip()
        )
    if settings.admin_trust_proxy_headers:
        return default_trusted_proxy_networks()
    return ()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of the configured trusted-proxy boundary. Header chains are parsed from the
    right (closest peer) to the left; the first untrusted hop is the client.
    """
    peer = request.client.host if request.client is not None else None
    resolved_peer = _resolved_peer_address(peer)
    normalized_peer = normalize_client_address(peer)

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(resolved_peer, SourceResolutionPath.PEER_DIRECT)

    trusted_networks = _trusted_networks_for_settings(settings)
    if not trusted_networks:
        return ClientSourceResolution(resolved_peer, SourceResolutionPath.PEER_FALLBACK)

    if not is_trusted_proxy_address(normalized_peer, trusted_networks):
        _maybe_log_invalid_forwarding("untrusted_immediate_peer")
        return ClientSourceResolution(resolved_peer, SourceResolutionPath.PEER_DIRECT)

    xff_value = request.headers.get("x-forwarded-for", "")
    if xff_value:
        xff_client = _client_from_x_forwarded_for(xff_value, trusted_networks=trusted_networks)
        if xff_client is not None:
            return ClientSourceResolution(xff_client, SourceResolutionPath.X_FORWARDED_FOR)
        _maybe_log_invalid_forwarding("invalid_x_forwarded_for")

    cf_value = request.headers.get("cf-connecting-ip", "")
    if cf_value and xff_value:
        xff_hosts = _parse_x_forwarded_for_hosts(xff_value) or []
        if _cloudflare_edge_seen(xff_hosts, trusted_networks=trusted_networks):
            cf_client = normalize_client_address(cf_value)
            if cf_client is not None:
                return ClientSourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)
        _maybe_log_invalid_forwarding("rejected_cf_connecting_ip")

    forwarded_value = request.headers.get("forwarded", "")
    if forwarded_value:
        forwarded_client = _parse_forwarded_header(forwarded_value)
        if forwarded_client is not None:
            return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED)
        _maybe_log_invalid_forwarding("invalid_forwarded")

    return ClientSourceResolution(resolved_peer, SourceResolutionPath.PEER_FALLBACK)


def _maybe_log_invalid_forwarding(reason: str) -> None:
    global _invalid_telemetry_last_logged, _invalid_telemetry_suppressed

    now = time.monotonic()
    if now - _invalid_telemetry_last_logged < _INVALID_TELEMETRY_INTERVAL_SECONDS:
        _invalid_telemetry_suppressed += 1
        return

    extra: dict[str, object] = {
        "source_resolution_path": SourceResolutionPath.INVALID_FORWARDING.value,
        "invalid_forwarding_reason": reason,
    }
    if _invalid_telemetry_suppressed:
        extra["suppressed_since_last"] = _invalid_telemetry_suppressed
    _logger.info("Admin login source forwarding rejected", extra=extra)
    _invalid_telemetry_last_logged = now
    _invalid_telemetry_suppressed = 0


def reset_proxy_trust_telemetry() -> None:
    """Reset sampled invalid-forwarding telemetry counters (tests only)."""
    global _invalid_telemetry_last_logged, _invalid_telemetry_suppressed
    _invalid_telemetry_last_logged = 0.0
    _invalid_telemetry_suppressed = 0


def production_forwarded_allow_ips(settings: Settings) -> str:
    """Return the Uvicorn ``--forwarded-allow-ips`` value matching app trust."""
    if settings.admin_trusted_proxy_ips:
        return settings.admin_trusted_proxy_ips
    if settings.admin_trust_proxy_headers:
        return ",".join(
            _RENDER_INTERNAL_IPV4_CIDRS + _CLOUDFLARE_IPV4_CIDRS + _CLOUDFLARE_IPV6_CIDRS
        )
    return "127.0.0.1"
