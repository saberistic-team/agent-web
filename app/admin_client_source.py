"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Production request chain (documented in docs/ADMIN_AUTH.md):
# Browser -> Cloudflare edge -> Render load balancer -> Uvicorn worker.
MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_TELEMETRY_MIN_INTERVAL_SECONDS = 60.0

# Render private-network peers that terminate TLS before the app process.
DEFAULT_RENDER_TRUSTED_PROXY_IPS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)

# Uvicorn --forwarded-allow-ips must match the immediate-peer boundary above.
DEFAULT_RENDER_FORWARDED_ALLOW_IPS = ",".join(DEFAULT_RENDER_TRUSTED_PROXY_IPS)

_FORWARDED_FOR_VALUE = re.compile(r"^for=(?P<value>[^;]+)", re.IGNORECASE)
_LAST_INVALID_TELEMETRY_AT: dict[str, float] = {}


class SourceResolutionPath(StrEnum):
    DIRECT_PEER = "direct_peer"
    CF_CONNECTING_IP = "cf_connecting_ip"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED = "forwarded"
    TRUSTED_PEER_UNKNOWN = "trusted_peer_unknown"
    MALFORMED_FORWARDING = "malformed_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: SourceResolutionPath


@dataclass(frozen=True)
class TrustedProxyBoundary:
    immediate_peers: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    forward_hops: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address, ...]

    def contains(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        for network in self.immediate_peers:
            if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                if address in network:
                    return True
            elif address == network:
                return True
        for network in self.forward_hops:
            if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                if address in network:
                    return True
            elif address == network:
                return True
        return False

    def immediate_peer_trusted(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        for network in self.immediate_peers:
            if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                if address in network:
                    return True
            elif address == network:
                return True
        return False


def parse_trusted_proxy_boundary(
    *,
    immediate_peer_spec: str,
    forward_hop_spec: str = "",
) -> TrustedProxyBoundary:
    immediate_peers = _parse_network_spec(immediate_peer_spec)
    forward_hops = _parse_network_spec(forward_hop_spec)
    return TrustedProxyBoundary(immediate_peers=immediate_peers, forward_hops=forward_hops)


def trusted_proxy_boundary_for_settings(settings: Settings) -> TrustedProxyBoundary | None:
    if settings.admin_trusted_proxy_ips:
        return parse_trusted_proxy_boundary(
            immediate_peer_spec=settings.admin_trusted_proxy_ips,
            forward_hop_spec=settings.admin_trusted_forward_proxy_ips,
        )
    if settings.admin_trust_proxy_headers:
        return parse_trusted_proxy_boundary(
            immediate_peer_spec=",".join(DEFAULT_RENDER_TRUSTED_PROXY_IPS),
            forward_hop_spec=settings.admin_trusted_forward_proxy_ips,
        )
    return None


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer = _peer_address(request)
    boundary = trusted_proxy_boundary_for_settings(settings)

    if boundary is None:
        source = _direct_peer_source(request)
        _record_source_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=source, path=SourceResolutionPath.DIRECT_PEER)

    if peer is None or not boundary.immediate_peer_trusted(peer):
        source = _direct_peer_source(request)
        _record_source_resolution_telemetry(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=source, path=SourceResolutionPath.DIRECT_PEER)

    cf_source = _resolve_cf_connecting_ip(request, boundary)
    if cf_source is not None:
        _record_source_resolution_telemetry(SourceResolutionPath.CF_CONNECTING_IP)
        return ClientSourceResolution(source=cf_source, path=SourceResolutionPath.CF_CONNECTING_IP)

    xff_source = _resolve_forwarded_chain(
        _xff_chain(request),
        boundary,
        path=SourceResolutionPath.X_FORWARDED_FOR,
    )
    if xff_source is not None:
        _record_source_resolution_telemetry(SourceResolutionPath.X_FORWARDED_FOR)
        return ClientSourceResolution(source=xff_source, path=SourceResolutionPath.X_FORWARDED_FOR)

    forwarded_source = _resolve_forwarded_chain(
        _forwarded_header_chain(request),
        boundary,
        path=SourceResolutionPath.FORWARDED,
    )
    if forwarded_source is not None:
        _record_source_resolution_telemetry(SourceResolutionPath.FORWARDED)
        return ClientSourceResolution(source=forwarded_source, path=SourceResolutionPath.FORWARDED)

    if _has_untrusted_forwarding_attempt(request):
        _record_source_resolution_telemetry(
            SourceResolutionPath.MALFORMED_FORWARDING,
            invalid=True,
        )
    else:
        _record_source_resolution_telemetry(SourceResolutionPath.TRUSTED_PEER_UNKNOWN)
    return ClientSourceResolution(
        source="unknown",
        path=SourceResolutionPath.TRUSTED_PEER_UNKNOWN,
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Backward-compatible wrapper returning only the resolved source string."""
    return resolve_admin_login_client_source(request, settings).source


def _peer_address(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if request.client is None:
        return None
    return _parse_ip(request.client.host)


def _direct_peer_source(request: Request) -> str:
    if request.client is None:
        return "unknown"
    parsed = _parse_ip(request.client.host)
    if parsed is not None:
        return _format_address(parsed)
    host = request.client.host.strip()
    return host or "unknown"


def _resolve_cf_connecting_ip(
    request: Request,
    boundary: TrustedProxyBoundary,
) -> str | None:
    """Accept CF-Connecting-IP only for requests that also carry Cloudflare edge markers."""
    raw_cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if not raw_cf_ip:
        return None
    if not request.headers.get("cf-ray", "").strip():
        return None

    parsed_cf_ip = _parse_ip(raw_cf_ip)
    if parsed_cf_ip is None:
        return None

    xff_chain = _xff_chain(request)
    if not xff_chain:
        return None
    walked = _walk_trusted_forward_chain(xff_chain, boundary)
    if walked is None:
        return None
    if _format_address(parsed_cf_ip) != walked:
        return None
    return _format_address(parsed_cf_ip)


def _resolve_forwarded_chain(
    chain: list[str],
    boundary: TrustedProxyBoundary,
    *,
    path: SourceResolutionPath,
) -> str | None:
    if not chain:
        return None
    if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
        _record_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING, invalid=True)
        return None
    if len(chain) == 1:
        _record_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING, invalid=True)
        return None
    walked = _walk_trusted_forward_chain(chain, boundary)
    if walked is None:
        _record_source_resolution_telemetry(SourceResolutionPath.MALFORMED_FORWARDING, invalid=True)
        return None
    _ = path
    return walked


def _walk_trusted_forward_chain(
    chain: list[str],
    boundary: TrustedProxyBoundary,
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping configured trusted hops."""
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for hop in chain:
        address = _parse_ip(hop)
        if address is None:
            return None
        parsed.append(address)

    for address in reversed(parsed):
        if boundary.contains(address):
            continue
        return _format_address(address)
    return None


def _xff_chain(request: Request) -> list[str]:
    raw = request.headers.get("x-forwarded-for", "")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _forwarded_header_chain(request: Request) -> list[str]:
    raw = request.headers.get("forwarded", "")
    if not raw:
        return []
    values: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        match = _FORWARDED_FOR_VALUE.search(entry)
        if match is None:
            return []
        value = match.group("value").strip().strip('"')
        if value.startswith("[") and "]" in value:
            host_port = value[1 : value.index("]")]
            values.append(host_port)
            continue
        host_port = value.split(":", 1)[0]
        values.append(host_port.strip())
    return values


def _has_untrusted_forwarding_attempt(request: Request) -> bool:
    return any(
        request.headers.get(header_name, "").strip()
        for header_name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _parse_network_spec(spec: str) -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address,
    ...,
]:
    entries: list[
        ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address
    ] = []
    for item in spec.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                entries.append(ipaddress.ip_network(candidate, strict=False))
            else:
                entries.append(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return tuple(entries)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.split(":", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _format_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _record_source_resolution_telemetry(
    path: SourceResolutionPath,
    *,
    invalid: bool = False,
) -> None:
    if invalid:
        now = time.monotonic()
        last_logged = _LAST_INVALID_TELEMETRY_AT.get(path.value, 0.0)
        if now - last_logged < _INVALID_TELEMETRY_MIN_INTERVAL_SECONDS:
            return
        _LAST_INVALID_TELEMETRY_AT[path.value] = now
        _logger.info(
            "Admin login client source rejected forwarding data",
            extra={"source_resolution_path": path.value, "forwarding_rejected": True},
        )
        return
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_resolution_path": path.value},
    )


def reset_source_resolution_telemetry_for_tests() -> None:
    """Clear sampled invalid-forwarding telemetry timestamps (tests only)."""
    _LAST_INVALID_TELEMETRY_AT.clear()
