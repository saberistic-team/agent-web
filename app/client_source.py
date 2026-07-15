"""Verified-hop client source resolution for admin login rate limiting."""

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

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/) — used only
# to skip trusted hops when ``admin_trust_cloudflare_edge`` is enabled.
_CLOUDFLARE_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
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
)
_CLOUDFLARE_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNKNOWN_SOURCE = "unknown"

# RFC 7239 Forwarded: for=192.0.2.60 or for="[2001:db8::1]:443"
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]|([^";,\s]+))',
    re.IGNORECASE,
)

_logger = logging.getLogger(__name__)
_spoof_telemetry_lock = Lock()
_spoof_telemetry_window_start = 0.0
_spoof_telemetry_count = 0
_SPOOF_TELEMETRY_WINDOW_SECONDS = 60.0
_SPOOF_TELEMETRY_MAX_PER_WINDOW = 20


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and non-sensitive telemetry metadata."""

    source: str
    path: str
    header_family: str | None = None
    untrusted_header_attempt: bool = False


def deployment_proxy_trust_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive snapshot for /health and deploy verification."""
    return {
        "proxy_trust_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_network_count": len(settings.admin_trusted_proxy_networks),
        "cloudflare_edge_trust_enabled": settings.admin_trust_cloudflare_edge,
        "resolution_strategy": "verified_hop_parse",
        "uvicorn_forwarded_allow_ips": settings.uvicorn_forwarded_allow_ips,
    }


def resolve_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    immediate_peer = _immediate_peer(request)
    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="immediate_peer",
        )
        _maybe_emit_untrusted_header_telemetry(request, resolution)
        return resolution

    trusted_networks = _trusted_networks(settings)
    if not trusted_networks:
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="immediate_peer_no_trusted_networks",
        )
        _maybe_emit_untrusted_header_telemetry(request, resolution)
        return resolution

    immediate_addr = _parse_ip(immediate_peer)
    if immediate_addr is None or not _address_in_networks(immediate_addr, trusted_networks):
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="immediate_peer_untrusted",
            untrusted_header_attempt=_has_forwarding_headers(request),
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _parse_x_forwarded_for(xff_raw)
    if xff_raw.strip() and xff_chain is None:
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="xff_malformed_or_overlong",
            header_family="x-forwarded-for",
            untrusted_header_attempt=True,
        )
        _emit_resolution_telemetry(resolution)
        return resolution
    if xff_chain is not None:
        client = _client_from_trusted_chain(xff_chain, trusted_networks)
        if client is not None:
            resolution = ClientSourceResolution(
                source=client,
                path="xff_right_to_left",
                header_family="x-forwarded-for",
            )
            _emit_resolution_telemetry(resolution)
            return resolution
        if xff_chain:
            cf_header = request.headers.get("cf-connecting-ip", "").strip()
            if cf_header and _cloudflare_hop_verified(request, trusted_networks, settings):
                normalized = normalize_client_source(cf_header)
                if normalized != _UNKNOWN_SOURCE:
                    resolution = ClientSourceResolution(
                        source=normalized,
                        path="cf_connecting_ip_verified",
                        header_family="cf-connecting-ip",
                    )
                    _emit_resolution_telemetry(resolution)
                    return resolution
            resolution = ClientSourceResolution(
                source=immediate_peer,
                path="xff_all_trusted_hops",
                header_family="x-forwarded-for",
            )
            _emit_resolution_telemetry(resolution)
            return resolution
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="xff_malformed_or_overlong",
            header_family="x-forwarded-for",
            untrusted_header_attempt=True,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    if cf_header and _cloudflare_hop_verified(request, trusted_networks, settings):
        normalized = normalize_client_source(cf_header)
        if normalized != _UNKNOWN_SOURCE:
            resolution = ClientSourceResolution(
                source=normalized,
                path="cf_connecting_ip_verified",
                header_family="cf-connecting-ip",
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))
    if forwarded_chain is not None:
        client = _client_from_trusted_chain(forwarded_chain, trusted_networks)
        if client is not None:
            resolution = ClientSourceResolution(
                source=client,
                path="forwarded_rfc7239",
                header_family="forwarded",
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    if _has_forwarding_headers(request):
        resolution = ClientSourceResolution(
            source=immediate_peer,
            path="forwarding_headers_ignored",
            untrusted_header_attempt=True,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    resolution = ClientSourceResolution(
        source=immediate_peer,
        path="immediate_peer_trusted",
    )
    _emit_resolution_telemetry(resolution)
    return resolution


def normalize_client_source(raw: str) -> str:
    """Normalize IPv4/IPv6 client material deterministically."""
    candidate = raw.strip()
    if not candidate:
        return _UNKNOWN_SOURCE

    if candidate.startswith("[") and "]" in candidate:
        host_part, _, port_part = candidate[1:].partition("]")
        if port_part.startswith(":") and port_part[1:].isdigit():
            candidate = host_part
        else:
            candidate = host_part

    if candidate.count(":") == 1 and candidate.rsplit(":", 1)[-1].isdigit():
        host, port = candidate.rsplit(":", 1)
        if host.count(":") == 0:
            candidate = host

    parsed = _parse_ip(candidate)
    if parsed is None:
        return _UNKNOWN_SOURCE

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for limiter key material."""
    return resolve_client_source(request, settings).source


def _immediate_peer(request: Request) -> str:
    if request.client is None:
        return _UNKNOWN_SOURCE
    return normalize_client_source(request.client.host)


def _trusted_networks(settings: Settings) -> tuple[ipaddress._BaseNetwork, ...]:
    networks = list(settings.admin_trusted_proxy_networks)
    if settings.admin_trust_cloudflare_edge:
        networks.extend(_CLOUDFLARE_IPV4_NETWORKS)
        networks.extend(_CLOUDFLARE_IPV6_NETWORKS)
    return tuple(networks)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress._BaseNetwork],
) -> bool:
    for network in networks:
        if address in network:
            return True
    return False


def _parse_x_forwarded_for(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        return None
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    normalized: list[str] = []
    for part in parts:
        source = normalize_client_source(part)
        if source == _UNKNOWN_SOURCE:
            return None
        normalized.append(source)
    return normalized


def _parse_forwarded_header(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    matches = _FORWARDED_FOR_RE.findall(raw)
    if not matches:
        return None
    chain: list[str] = []
    for bracketed, plain in matches:
        candidate = bracketed or plain
        source = normalize_client_source(candidate)
        if source == _UNKNOWN_SOURCE:
            return None
        chain.append(source)
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return chain


def _client_from_trusted_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
) -> str | None:
    for hop in reversed(chain):
        addr = _parse_ip(hop)
        if addr is None:
            return None
        if _address_in_networks(addr, trusted_networks):
            continue
        return hop
    return None


def _cloudflare_hop_verified(
    request: Request,
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
    settings: Settings,
) -> bool:
    if not settings.admin_trust_cloudflare_edge:
        return False
    xff_chain = _parse_x_forwarded_for(request.headers.get("x-forwarded-for", ""))
    if not xff_chain:
        return False
    cf_networks = _CLOUDFLARE_IPV4_NETWORKS + _CLOUDFLARE_IPV6_NETWORKS
    for hop in xff_chain:
        addr = _parse_ip(hop)
        if addr is not None and _address_in_networks(addr, cf_networks):
            return True
    del trusted_networks
    return False


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _maybe_emit_untrusted_header_telemetry(
    request: Request,
    resolution: ClientSourceResolution,
) -> None:
    if _has_forwarding_headers(request):
        spoof_resolution = ClientSourceResolution(
            source=resolution.source,
            path=resolution.path,
            untrusted_header_attempt=True,
        )
        _emit_resolution_telemetry(spoof_resolution)


def _emit_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    if resolution.untrusted_header_attempt:
        if not _allow_spoof_telemetry():
            return
        _logger.warning(
            "Admin login client source ignored forwarding headers",
            extra={
                "client_source_path": resolution.path,
                "client_source_header_family": resolution.header_family,
                "untrusted_header_attempt": True,
            },
        )
        return

    _logger.debug(
        "Admin login client source resolved",
        extra={
            "client_source_path": resolution.path,
            "client_source_header_family": resolution.header_family,
        },
    )


def _allow_spoof_telemetry() -> bool:
    global _spoof_telemetry_window_start, _spoof_telemetry_count
    now = time.monotonic()
    with _spoof_telemetry_lock:
        if now - _spoof_telemetry_window_start >= _SPOOF_TELEMETRY_WINDOW_SECONDS:
            _spoof_telemetry_window_start = now
            _spoof_telemetry_count = 0
        if _spoof_telemetry_count >= _SPOOF_TELEMETRY_MAX_PER_WINDOW:
            return False
        _spoof_telemetry_count += 1
        return True


def reset_spoof_telemetry_for_tests() -> None:
    """Reset rate-limited spoof telemetry counters (tests only)."""
    global _spoof_telemetry_window_start, _spoof_telemetry_count
    with _spoof_telemetry_lock:
        _spoof_telemetry_window_start = 0.0
        _spoof_telemetry_count = 0
