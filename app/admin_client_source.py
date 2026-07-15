"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative cap so overlong chains cannot allocate unbounded work.
MAX_FORWARDING_CHAIN_LENGTH = 32

# Sample invalid/untrusted forwarding telemetry (no raw addresses in logs).
_INVALID_FORWARDING_LOG_EVERY = 64

_PATH_DIRECT_PEER = "direct_peer"
_PATH_CF_CONNECTING_IP = "cf_connecting_ip"
_PATH_X_FORWARDED_FOR = "x_forwarded_for"
_PATH_FORWARDED = "forwarded"
_PATH_TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
_PATH_UNKNOWN = "unknown"

_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"\[([^\]]+)\]:(\d+)"|"([^"]+)"|([^;\s,]+))',
    re.IGNORECASE,
)

_telemetry_lock = threading.Lock()
_invalid_forwarding_attempts = 0


def reset_source_resolution_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _invalid_forwarding_attempts
    with _telemetry_lock:
        _invalid_forwarding_attempts = 0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source plus a privacy-safe telemetry path label."""

    source: str
    path: str
    rejected_untrusted_forwarding: bool = False


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated proxy CIDRs/addresses from ``ADMIN_TRUSTED_PROXY_IPS``."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                addr = ipaddress.ip_address(token)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(value: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) or return ``None`` when invalid."""
    candidate = value.strip()
    if not candidate:
        return None

    host = candidate
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return None
        host = host[1:closing]
    elif host.count(":") == 1 and "." in host:
        host, _port = host.rsplit(":", 1)
        if not _port.isdigit():
            return None

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return addr.compressed


def is_trusted_proxy_address(address: str, settings: Settings) -> bool:
    """Return whether ``address`` is inside the configured trusted-proxy boundary."""
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    for network in settings.admin_trusted_proxy_networks:
        if parsed in network:
            return True
    return False


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _materialize_peer_source(peer: str | None) -> str:
    """Use a normalized IP when possible; otherwise keep the direct peer label."""
    if not peer:
        return "unknown"
    normalized = normalize_client_address(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip().lower()
    return stripped or "unknown"


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("cf-connecting-ip", "x-forwarded-for", "forwarded")
    )


def _split_forwarding_chain(header_value: str) -> list[str]:
    parts = [segment.strip() for segment in header_value.split(",")]
    return [segment for segment in parts if segment]


def _client_from_x_forwarded_for(header_value: str, settings: Settings) -> str | None:
    hops = _split_forwarding_chain(header_value)
    if not hops:
        return None
    if len(hops) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for hop in reversed(hops):
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        if not is_trusted_proxy_address(normalized, settings):
            return normalized
    return None


def _client_from_forwarded(header_value: str, settings: Settings) -> str | None:
    matches = list(_FORWARDED_FOR_RE.finditer(header_value))
    if not matches:
        return None
    if len(matches) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    hops: list[str] = []
    for match in matches:
        hop = match.group(1) or match.group(2) or match.group(4) or match.group(5)
        if hop:
            hops.append(hop.strip())

    for hop in reversed(hops):
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        if not is_trusted_proxy_address(normalized, settings):
            return normalized
    return None


def _record_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    extra = {"admin_client_source_path": resolution.path}
    if resolution.rejected_untrusted_forwarding:
        global _invalid_forwarding_attempts
        with _telemetry_lock:
            _invalid_forwarding_attempts += 1
            attempt = _invalid_forwarding_attempts
        if attempt % _INVALID_FORWARDING_LOG_EVERY == 1:
            _logger.info(
                "Admin login ignored untrusted forwarding headers",
                extra={**extra, "sampled": True},
            )
        return
    _logger.debug("Admin login client source resolved", extra=extra)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Resolve the effective client source for admin login limiter buckets."""
    resolution = _resolve_admin_login_client_source(request, settings)
    _record_resolution_telemetry(resolution)
    return resolution.source


def _resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    peer = _peer_host(request)
    peer_source = _materialize_peer_source(peer)

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(source=peer_source, path=_PATH_DIRECT_PEER)

    peer_trusted = peer is not None and is_trusted_proxy_address(peer, settings)
    if not peer_trusted:
        rejected = _has_forwarding_headers(request)
        return ClientSourceResolution(
            source=peer_source,
            path=_PATH_DIRECT_PEER,
            rejected_untrusted_forwarding=rejected,
        )

    cf_header = request.headers.get("cf-connecting-ip")
    if cf_header:
        normalized = normalize_client_address(cf_header)
        if normalized is not None:
            return ClientSourceResolution(
                source=normalized,
                path=_PATH_CF_CONNECTING_IP,
            )

    xff_header = request.headers.get("x-forwarded-for")
    if xff_header:
        client = _client_from_x_forwarded_for(xff_header, settings)
        if client is not None:
            return ClientSourceResolution(source=client, path=_PATH_X_FORWARDED_FOR)
        return ClientSourceResolution(
            source="unknown",
            path=_PATH_UNKNOWN,
            rejected_untrusted_forwarding=True,
        )

    forwarded_header = request.headers.get("forwarded")
    if forwarded_header:
        client = _client_from_forwarded(forwarded_header, settings)
        if client is not None:
            return ClientSourceResolution(source=client, path=_PATH_FORWARDED)
        return ClientSourceResolution(
            source="unknown",
            path=_PATH_UNKNOWN,
            rejected_untrusted_forwarding=True,
        )

    return ClientSourceResolution(
        source=peer_source,
        path=_PATH_TRUSTED_PEER_FALLBACK,
    )


# Documented production uvicorn forwarded-header settings (see render.yaml).
PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"
PRODUCTION_TRUSTED_PROXY_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
)
PRODUCTION_UVICORN_START_COMMAND = (
    "uvicorn app.main:app --host 0.0.0.0 --port $PORT "
    f"--forwarded-allow-ips '{PRODUCTION_FORWARDED_ALLOW_IPS}'"
)
