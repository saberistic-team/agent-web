"""Trusted-proxy client source resolution for admin login rate limiting."""

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

MISSING_CLIENT_SOURCE = "unknown"
MAX_FORWARDING_CHAIN_LENGTH = 20
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0

_logger = logging.getLogger(__name__)
_telemetry_lock = Lock()
_last_invalid_telemetry_at: dict[str, float] = {}

# RFC 7239 Forwarded: for=...
_FORWARDED_FOR_RE = re.compile(
    r"for=(?:\"([^\"]+)\"|\[([^\]]+)\]|([^;,]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without raw forwarding metadata."""

    source: str
    path: str


def parse_trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs and host IPs into network objects."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                addr = ipaddress.ip_address(entry)
                prefix = 128 if addr.version == 6 else 32
                networks.append(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Return a deterministic IPv4/IPv6 string or None when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.count(":") == 1 and candidate.rsplit(":", 1)[0].count(":") == 0:
        host, _, port = candidate.rpartition(":")
        if port.isdigit():
            candidate = host
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if addr.version == 6:
        return addr.compressed
    return str(addr)


def _address_in_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        ip_obj = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(ip_obj in network for network in networks)


def _emit_resolution_telemetry(path: str, *, invalid: bool = False) -> None:
    extra = {"admin_client_source_path": path}
    if invalid:
        now = time.monotonic()
        with _telemetry_lock:
            last = _last_invalid_telemetry_at.get(path, 0.0)
            if now - last < _INVALID_TELEMETRY_INTERVAL_SECONDS:
                return
            _last_invalid_telemetry_at[path] = now
        _logger.info("Admin client source forwarding rejected", extra=extra)
        return
    _logger.debug("Admin client source resolved", extra=extra)


def _parse_x_forwarded_for(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_forwarded_header(raw: str) -> list[str]:
    addresses: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        candidate = match.group(1) or match.group(2) or match.group(3)
        if candidate:
            addresses.append(candidate.strip())
    return addresses


def _header_hops(request: Request) -> tuple[list[str], str]:
    """Return candidate hops and the header family used."""
    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        return _parse_forwarded_header(forwarded), "forwarded"
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return _parse_x_forwarded_for(xff), "x-forwarded-for"
    return [], "none"


def _append_immediate_peer(hops: list[str], peer: str | None) -> list[str]:
    if peer is None:
        return hops
    normalized_peer = normalize_ip_address(peer)
    if normalized_peer is None:
        return hops
    if hops:
        last = normalize_ip_address(hops[-1])
        if last == normalized_peer:
            return hops
    return [*hops, normalized_peer]


def _select_client_from_trusted_chain(
    hops: Iterable[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    if not normalized_hops:
        return None
    if len(normalized_hops) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for hop in reversed(normalized_hops):
        if _address_in_networks(hop, trusted_networks):
            continue
        return hop
    return None


def _cloudflare_hop_present(
    hops: Iterable[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    return any(
        _address_in_networks(hop, cloudflare_networks)
        for hop in hops
        if normalize_ip_address(hop) is not None
    )


def _resolve_from_cf_connecting_ip(
    request: Request,
    *,
    header_hops: list[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    raw = request.headers.get("cf-connecting-ip", "").strip()
    if not raw:
        return None
    if not _cloudflare_hop_present(header_hops, cloudflare_networks):
        return None
    return normalize_ip_address(raw)


def _peer_source(peer: str | None) -> str | None:
    if peer is None:
        return None
    normalized = normalize_ip_address(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip()
    return stripped or None


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer = request.client.host if request.client is not None else None
    normalized_peer = _peer_source(peer)

    if normalized_peer is None:
        _emit_resolution_telemetry("peer_missing")
        return ClientSourceResolution(source=MISSING_CLIENT_SOURCE, path="peer_missing")

    peer_is_ip = normalize_ip_address(peer or "") is not None

    if not settings.admin_trust_proxy_headers:
        _emit_resolution_telemetry("direct_peer")
        return ClientSourceResolution(source=normalized_peer, path="direct_peer")

    trusted_networks = settings.admin_trusted_proxy_networks
    if not trusted_networks:
        _emit_resolution_telemetry("direct_peer_no_trusted_boundary")
        return ClientSourceResolution(source=normalized_peer, path="direct_peer_no_trusted_boundary")

    if not peer_is_ip or not _address_in_networks(normalized_peer, trusted_networks):
        if _header_hops(request)[0]:
            _emit_resolution_telemetry("untrusted_peer_headers_ignored", invalid=True)
        else:
            _emit_resolution_telemetry("direct_peer")
        return ClientSourceResolution(source=normalized_peer, path="direct_peer")

    header_hops, header_family = _header_hops(request)
    if not header_hops:
        _emit_resolution_telemetry("trusted_peer_no_forwarding")
        return ClientSourceResolution(source=normalized_peer, path="trusted_peer_no_forwarding")

    chain = _append_immediate_peer(header_hops, normalized_peer)
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        _emit_resolution_telemetry("overlong_forwarding_chain", invalid=True)
        return ClientSourceResolution(source=MISSING_CLIENT_SOURCE, path="overlong_forwarding_chain")

    cf_source = _resolve_from_cf_connecting_ip(
        request,
        header_hops=chain,
        cloudflare_networks=settings.admin_cloudflare_proxy_networks,
    )
    if cf_source is not None:
        path = f"trusted_cf_connecting_ip_{header_family.replace('-', '_')}"
        _emit_resolution_telemetry(path)
        return ClientSourceResolution(source=cf_source, path=path)

    client = _select_client_from_trusted_chain(chain, trusted_networks=trusted_networks)
    if client is None:
        _emit_resolution_telemetry("malformed_forwarding_chain", invalid=True)
        return ClientSourceResolution(source=MISSING_CLIENT_SOURCE, path="malformed_forwarding_chain")

    if len(header_hops) == 1 and client == normalize_ip_address(header_hops[0]):
        _emit_resolution_telemetry("single_hop_forwarding_ambiguous", invalid=True)
        return ClientSourceResolution(
            source=MISSING_CLIENT_SOURCE,
            path="single_hop_forwarding_ambiguous",
        )

    path = f"trusted_chain_{header_family.replace('-', '_')}"
    _emit_resolution_telemetry(path)
    return ClientSourceResolution(source=client, path=path)


def resolve_client_source(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for limiter key material."""
    return resolve_admin_login_client_source(request, settings).source
