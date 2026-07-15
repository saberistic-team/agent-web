"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Render internal load-balancer networks (also used for Uvicorn forwarded-allow-ips).
RENDER_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
)

# Public Cloudflare IPv4 edge ranges (https://www.cloudflare.com/ips-v4).
DEFAULT_CLOUDFLARE_EDGE_CIDRS = (
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

MAX_FORWARDING_CHAIN_LENGTH = 32
_MISSING_PEER_SOURCE = "unknown"
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_TELEMETRY_MAX_KEYS = 64

_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_telemetry_last_logged: dict[str, float] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str
    rejected_forwarding: bool = False



def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address in network for network in networks)


def normalize_ip_literal(value: str) -> str | None:
    """Normalize IPv4/IPv6 literals; return None for malformed or overlong input."""
    candidate = value.strip()
    if not candidate or len(candidate) > 128:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate[1:].partition("]")
        if remainder and not remainder.startswith(":"):
            return None
        candidate = host
    elif candidate.count(":") == 1 and "." in candidate:
        host, sep, port = candidate.rpartition(":")
        if sep and port.isdigit():
            candidate = host

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return address.compressed
    return str(address)


def immediate_peer_host(request: Request) -> str | None:
    """Return the raw ASGI peer captured before Uvicorn proxy-header rewriting."""
    peer = getattr(request.state, "asgi_peer_host", None)
    if isinstance(peer, str) and peer.strip():
        return peer.strip()
    if request.client is not None and request.client.host:
        return request.client.host
    return None


def _parse_forwarded_header(value: str) -> str | None:
    for entry in value.split(","):
        part = entry.strip()
        if not part:
            continue
        for token in part.split(";"):
            token = token.strip()
            if token.lower().startswith("for="):
                raw = token[4:].strip().strip('"')
                if raw.lower() in {"unknown", "_hidden"}:
                    continue
                if raw.startswith("[") and raw.endswith("]"):
                    raw = raw[1:-1]
                return normalize_ip_literal(raw)
    return None


def _parse_forwarding_chain(header_value: str) -> list[str]:
    if not header_value or len(header_value) > 2048:
        return []
    parts = [segment.strip() for segment in header_value.split(",")]
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return []
    chain: list[str] = []
    for part in parts:
        if not part:
            continue
        normalized = normalize_ip_literal(part)
        if normalized is None:
            return []
        chain.append(normalized)
    return chain


def _client_from_trusted_xff_chain(
    chain: list[str],
    trusted_hops: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    for hop in reversed(chain):
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return None
        if not _address_in_networks(address, trusted_hops):
            return hop
    return None


def _cloudflare_edge_seen(
    chain: list[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    for hop in chain:
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if _address_in_networks(address, cloudflare_networks):
            return True
    return False


def _record_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    key = resolution.path
    if resolution.rejected_forwarding:
        key = f"{resolution.path}:rejected"
    now = time.monotonic()
    should_log = False
    with _telemetry_lock:
        last_logged = _telemetry_last_logged.get(key)
        if last_logged is None or now - last_logged >= _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            _telemetry_last_logged[key] = now
            should_log = True
            if len(_telemetry_last_logged) > _TELEMETRY_MAX_KEYS:
                oldest_key = min(_telemetry_last_logged, key=_telemetry_last_logged.get)
                _telemetry_last_logged.pop(oldest_key, None)
    if not should_log:
        return
    level = logging.INFO
    if resolution.rejected_forwarding or resolution.path == "invalid_forwarding":
        level = logging.WARNING
    _logger.log(
        level,
        "Admin login client source resolved",
        extra={
            "source_resolution_path": resolution.path,
            "forwarding_rejected": resolution.rejected_forwarding,
        },
    )


def _peer_source_identity(peer_raw: str) -> str:
    normalized = normalize_ip_literal(peer_raw)
    if normalized is not None:
        return normalized
    stripped = peer_raw.strip()
    return stripped if stripped else _MISSING_PEER_SOURCE


def resolve_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_raw = immediate_peer_host(request)
    if peer_raw is None:
        resolution = ClientSourceResolution(source=_MISSING_PEER_SOURCE, path="missing_peer")
        _record_resolution_telemetry(resolution)
        return resolution

    peer_source = _peer_source_identity(peer_raw)

    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(source=peer_source, path="direct_peer")
        _record_resolution_telemetry(resolution)
        return resolution

    trusted_hops = settings.admin_trusted_proxy_networks
    if not trusted_hops:
        resolution = ClientSourceResolution(
            source=peer_source,
            path="direct_peer",
            rejected_forwarding=bool(request.headers.get("x-forwarded-for")),
        )
        _record_resolution_telemetry(resolution)
        return resolution

    peer = normalize_ip_literal(peer_raw)
    if peer is None:
        rejected = any(
            request.headers.get(name)
            for name in (
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "x-real-ip",
            )
        )
        resolution = ClientSourceResolution(
            source=peer_source,
            path="untrusted_forwarding_rejected",
            rejected_forwarding=rejected,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        resolution = ClientSourceResolution(source=_MISSING_PEER_SOURCE, path="invalid_forwarding")
        _record_resolution_telemetry(resolution)
        return resolution

    if not _address_in_networks(peer_address, trusted_hops):
        rejected = any(
            request.headers.get(name)
            for name in (
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "x-real-ip",
            )
        )
        resolution = ClientSourceResolution(
            source=peer_source,
            path="untrusted_forwarding_rejected",
            rejected_forwarding=rejected,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    xff_chain = _parse_forwarding_chain(request.headers.get("x-forwarded-for", ""))
    cloudflare_networks = settings.admin_cloudflare_edge_networks

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_candidate = normalize_ip_literal(cf_header) if cf_header else None
    if (
        cf_candidate
        and cloudflare_networks
        and xff_chain
        and _cloudflare_edge_seen(xff_chain, cloudflare_networks)
    ):
        resolution = ClientSourceResolution(source=cf_candidate, path="cf_connecting_ip")
        _record_resolution_telemetry(resolution)
        return resolution

    if xff_chain:
        client_from_xff = _client_from_trusted_xff_chain(xff_chain, trusted_hops)
        if client_from_xff:
            resolution = ClientSourceResolution(source=client_from_xff, path="xff_right_to_left")
            _record_resolution_telemetry(resolution)
            return resolution
        resolution = ClientSourceResolution(
            source=peer_source, path="invalid_forwarding", rejected_forwarding=True
        )
        _record_resolution_telemetry(resolution)
        return resolution

    forwarded_header = request.headers.get("forwarded", "")
    forwarded_client = _parse_forwarded_header(forwarded_header) if forwarded_header else None
    if forwarded_client:
        resolution = ClientSourceResolution(source=forwarded_client, path="forwarded_header")
        _record_resolution_telemetry(resolution)
        return resolution

    resolution = ClientSourceResolution(source=peer_source, path="trusted_peer_fallback")
    _record_resolution_telemetry(resolution)
    return resolution


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized client source string used by the login limiter."""
    return resolve_client_source(request, settings).source


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled telemetry counters (tests only)."""
    with _telemetry_lock:
        _telemetry_last_logged.clear()


def proxy_trust_health_summary(settings: Settings) -> dict[str, object]:
    """Bounded deployment summary for /health without exposing addresses or CIDRs."""
    return {
        "proxy_headers_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_network_count": len(settings.admin_trusted_proxy_networks),
        "cloudflare_edge_network_count": len(settings.admin_cloudflare_edge_networks),
        "forwarded_allow_ips_configured": bool(settings.admin_forwarded_allow_ips.strip()),
    }
