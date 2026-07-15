"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Public Cloudflare anycast ranges (https://www.cloudflare.com/ips/) used only to
# strip edge hops from forwarding chains — not as proof of origin by themselves.
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

_DEFAULT_IMMEDIATE_PEER_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "127.0.0.1",
    "::1",
)

_DEFAULT_FORWARDED_HOP_CIDRS: tuple[str, ...] = (
    *_DEFAULT_IMMEDIATE_PEER_CIDRS,
    *_CLOUDFLARE_IPV4_CIDRS,
    *_CLOUDFLARE_IPV6_CIDRS,
)

_DEFAULT_FORWARDED_MAX_HOPS = 32
_TELEMETRY_SAMPLE_WINDOW_SECONDS = 60
_TELEMETRY_SAMPLE_MAX_EVENTS = 20

_telemetry_lock = threading.Lock()
_telemetry_counts: dict[str, tuple[int, float]] = {}


class ClientSourceResolutionPath(str, Enum):
    DIRECT_PEER = "direct_peer"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_TRUSTED_CHAIN = "forwarded_trusted_chain"
    CF_CONNECTING_IP_VERIFIED = "cf_connecting_ip_verified"
    PEER_FALLBACK = "peer_fallback"
    UNKNOWN_PEER = "unknown_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source plus non-sensitive observability metadata."""

    source: str
    path: ClientSourceResolutionPath
    forwarding_rejected: bool = False
    reject_reason: str | None = None


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer = _immediate_peer(request)
    if peer is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path=ClientSourceResolutionPath.UNKNOWN_PEER,
        )
        _emit_resolution_telemetry(resolution, settings)
        return resolution

    if not settings.admin_trust_proxy_headers:
        resolution = _reject_forwarding(
            peer,
            reason="proxy_trust_disabled",
            header_present=_forwarding_headers_present(request),
        )
        _emit_resolution_telemetry(resolution, settings)
        return resolution

    peer_networks = _networks_for(settings.admin_trusted_proxy_cidrs, _DEFAULT_IMMEDIATE_PEER_CIDRS)
    if not _address_in_networks(peer, peer_networks):
        resolution = _reject_forwarding(
            peer,
            reason="untrusted_immediate_peer",
            header_present=_forwarding_headers_present(request),
        )
        _emit_resolution_telemetry(resolution, settings)
        return resolution

    hop_networks = _networks_for(
        settings.admin_forwarded_trusted_hop_cidrs,
        _DEFAULT_FORWARDED_HOP_CIDRS,
    )

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        chain = _parse_forwarded_chain(xff_header, settings.admin_forwarded_max_hops)
        if chain is None:
            resolution = _reject_forwarding(
                peer,
                reason="malformed_x_forwarded_for",
                header_present=True,
            )
            _emit_resolution_telemetry(resolution, settings)
            return resolution
        resolved = _resolve_from_trusted_chain(chain, hop_networks)
        if resolved is not None:
            resolution = ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.XFF_TRUSTED_CHAIN,
            )
            _emit_resolution_telemetry(resolution, settings)
            return resolution

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        chain = _parse_forwarded_header(forwarded_header, settings.admin_forwarded_max_hops)
        if chain is None:
            resolution = _reject_forwarding(
                peer,
                reason="malformed_forwarded",
                header_present=True,
            )
            _emit_resolution_telemetry(resolution, settings)
            return resolution
        resolved = _resolve_from_trusted_chain(chain, hop_networks)
        if resolved is not None:
            resolution = ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.FORWARDED_TRUSTED_CHAIN,
            )
            _emit_resolution_telemetry(resolution, settings)
            return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and _cloudflare_edge_present_in_xff(xff_header, hop_networks):
        normalized = _normalize_ip(cf_header)
        if normalized is None:
            resolution = _reject_forwarding(
                peer,
                reason="malformed_cf_connecting_ip",
                header_present=True,
            )
        else:
            resolution = ClientSourceResolution(
                source=normalized,
                path=ClientSourceResolutionPath.CF_CONNECTING_IP_VERIFIED,
            )
        _emit_resolution_telemetry(resolution, settings)
        return resolution

    if _forwarding_headers_present(request):
        _record_sampled_event("forwarding_ignored_no_client")

    resolution = ClientSourceResolution(
        source=peer,
        path=ClientSourceResolutionPath.PEER_FALLBACK,
        forwarding_rejected=_forwarding_headers_present(request),
        reject_reason="no_client_in_trusted_chain" if _forwarding_headers_present(request) else None,
    )
    _emit_resolution_telemetry(resolution, settings)
    return resolution


def client_ip_from_resolution(resolution: ClientSourceResolution) -> str:
    return resolution.source


def _reject_forwarding(
    peer: str,
    *,
    reason: str,
    header_present: bool,
) -> ClientSourceResolution:
    if header_present:
        _record_sampled_event(f"forwarding_rejected:{reason}")
    return ClientSourceResolution(
        source=peer,
        path=ClientSourceResolutionPath.DIRECT_PEER,
        forwarding_rejected=header_present,
        reject_reason=reason if header_present else None,
    )


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    if not host:
        return None
    normalized = _normalize_ip(host)
    if normalized is not None:
        return normalized
    return host.lower()


def _forwarding_headers_present(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _parse_forwarded_chain(header: str, max_hops: int) -> list[str] | None:
    parts = [part.strip() for part in header.split(",")]
    if not parts or len(parts) > max_hops:
        return None
    chain: list[str] = []
    for part in parts:
        if not part:
            return None
        normalized = _normalize_ip(part)
        if normalized is None:
            return None
        chain.append(normalized)
    return chain


def _parse_forwarded_header(header: str, max_hops: int) -> list[str] | None:
    entries = [entry.strip() for entry in header.split(",") if entry.strip()]
    if not entries or len(entries) > max_hops:
        return None
    chain: list[str] = []
    for entry in entries:
        for_value = _extract_forwarded_for(entry)
        if for_value is None:
            return None
        chain.append(for_value)
    return chain


def _extract_forwarded_for(entry: str) -> str | None:
    for segment in entry.split(";"):
        segment = segment.strip()
        if not segment.lower().startswith("for="):
            continue
        value = segment[4:].strip().strip('"')
        if value.lower() == "unknown":
            return None
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return _normalize_ip(value)
    return None


def _resolve_from_trusted_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    idx = len(chain) - 1
    while idx >= 0 and _address_in_networks(chain[idx], trusted_networks):
        idx -= 1
    if idx < 0:
        return None
    for pos in range(idx):
        if _address_in_networks(chain[pos], trusted_networks):
            return None
    return chain[idx]


def _cloudflare_edge_present_in_xff(
    xff_header: str,
    hop_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not xff_header.strip():
        return False
    chain = _parse_forwarded_chain(xff_header, _DEFAULT_FORWARDED_MAX_HOPS)
    if chain is None:
        return False
    cf_networks = _networks_for("", _CLOUDFLARE_IPV4_CIDRS + _CLOUDFLARE_IPV6_CIDRS)
    for hop in chain:
        if _address_in_networks(hop, cf_networks):
            return True
    return False


def _normalize_ip(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            return None
        value = host
    elif value.count(":") == 1 and "." in value:
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


@lru_cache(maxsize=16)
def _networks_for(
    configured: str,
    defaults: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    specs = _split_cidr_specs(configured) or defaults
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for spec in specs:
        try:
            networks.append(ipaddress.ip_network(spec.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _split_cidr_specs(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _address_in_networks(
    address: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _emit_resolution_telemetry(
    resolution: ClientSourceResolution,
    settings: Settings,
) -> None:
    if not settings.admin_trust_proxy_headers:
        return
    extra = {
        "client_source_resolution": resolution.path.value,
        "forwarding_rejected": resolution.forwarding_rejected,
    }
    if resolution.reject_reason:
        extra["forwarding_reject_reason"] = resolution.reject_reason
    _logger.debug("Admin login client source resolved", extra=extra)


def _record_sampled_event(event_key: str) -> None:
    now = time.monotonic()
    with _telemetry_lock:
        count, window_start = _telemetry_counts.get(event_key, (0, now))
        if now - window_start >= _TELEMETRY_SAMPLE_WINDOW_SECONDS:
            count = 0
            window_start = now
        count += 1
        _telemetry_counts[event_key] = (count, window_start)
        if count > _TELEMETRY_SAMPLE_MAX_EVENTS:
            return
    _logger.info(
        "Admin login forwarding header telemetry",
        extra={"forwarding_event": event_key, "sampled": True},
    )


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled telemetry counters (tests only)."""
    with _telemetry_lock:
        _telemetry_counts.clear()
    _networks_for.cache_clear()
