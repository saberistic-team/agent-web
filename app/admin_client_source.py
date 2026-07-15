"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from fastapi import Request

from app.config import Settings

MAX_FORWARDED_CHAIN_LEN = 32
MAX_FORWARDED_HEADER_LEN = 2048
_UNKNOWN_SOURCE = "unknown"
_TELEMETRY_SAMPLE_INTERVAL = 60.0

_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_invalid_forwarding_samples: dict[str, float] = {}


class SourceResolutionPath(str, Enum):
    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_RFC7239 = "forwarded_rfc7239"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    MALFORMED_FORWARDING = "malformed_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: SourceResolutionPath
    invalid_forwarding: bool = False


def normalize_client_source(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 client source strings deterministically."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    if value.startswith("[") and "]" in value:
        host_part, _, port_part = value[1:].partition("]")
        if port_part.startswith(":") and port_part[1:].isdigit():
            value = host_part
        else:
            value = host_part
    elif value.count(":") == 1 and value.rsplit(":", 1)[-1].isdigit():
        host, port = value.rsplit(":", 1)
        if host.count(":") == 0:
            value = host

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv6Address):
        return address.compressed
    return str(address)


def is_address_in_cidrs(
    address: str,
    cidrs: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_client_source(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in cidrs)


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _parse_x_forwarded_for(header: str) -> list[str] | None:
    if len(header) > MAX_FORWARDED_HEADER_LEN:
        return None
    parts = [part.strip() for part in header.split(",")]
    if len(parts) > MAX_FORWARDED_CHAIN_LEN:
        return None

    normalized: list[str] = []
    for part in parts:
        if not part:
            continue
        candidate = normalize_client_source(part)
        if candidate is None:
            return None
        normalized.append(candidate)
    if not normalized:
        return None
    return normalized


_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|\[([^\]]+)\]|"([^"]+)"|([^;,\s]+))',
    re.IGNORECASE,
)


def _parse_forwarded_header(header: str) -> list[str] | None:
    if len(header) > MAX_FORWARDED_HEADER_LEN:
        return None
    normalized: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header):
        raw = next(group for group in match.groups() if group is not None)
        candidate = normalize_client_source(raw)
        if candidate is None:
            return None
        normalized.append(candidate)
    if not normalized:
        return None
    if len(normalized) > MAX_FORWARDED_CHAIN_LEN:
        return None
    return normalized


def _trusted_hop_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return settings.admin_trusted_proxy_cidrs + settings.admin_trusted_edge_cidrs


def _select_client_from_chain(
    chain: list[str],
    *,
    trusted_hops: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Walk X-Forwarded-For right-to-left, skipping verified proxy hops."""
    for hop in reversed(chain):
        if is_address_in_cidrs(hop, trusted_hops):
            continue
        return hop
    return None


def _record_invalid_forwarding_telemetry(reason: str, path: SourceResolutionPath) -> None:
    now = time.time()
    with _telemetry_lock:
        last_logged = _invalid_forwarding_samples.get(reason, 0.0)
        if now - last_logged < _TELEMETRY_SAMPLE_INTERVAL:
            return
        _invalid_forwarding_samples[reason] = now
    _logger.info(
        "Admin login client source rejected forwarding data",
        extra={
            "source_resolution_path": path.value,
            "forwarding_rejection_reason": reason,
        },
    )


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry timestamps (tests only)."""
    with _telemetry_lock:
        _invalid_forwarding_samples.clear()


def _peer_source(host: str | None) -> str:
    if not host:
        return _UNKNOWN_SOURCE
    normalized = normalize_client_source(host)
    if normalized is not None:
        return normalized
    stripped = host.strip().lower()
    return stripped or _UNKNOWN_SOURCE


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin login limiter source for one request."""
    peer_host = request.client.host if request.client else None
    peer = _peer_source(peer_host)

    if not settings.admin_trusted_proxy_cidrs:
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    if not is_address_in_cidrs(peer, settings.admin_trusted_proxy_cidrs):
        if _has_forwarding_headers(request):
            _record_invalid_forwarding_telemetry(
                "untrusted_peer_with_forwarding",
                SourceResolutionPath.UNTRUSTED_PEER,
            )
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_PEER,
            invalid_forwarding=_has_forwarding_headers(request),
        )

    trusted_hops = _trusted_hop_networks(settings)
    xff_raw = request.headers.get("x-forwarded-for", "").strip()
    xff_chain = _parse_x_forwarded_for(xff_raw) if xff_raw else None

    if xff_raw and xff_chain is None:
        _record_invalid_forwarding_telemetry("malformed_xff", SourceResolutionPath.MALFORMED_FORWARDING)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            invalid_forwarding=True,
        )

    if xff_chain:
        if len(xff_chain) == 1:
            _record_invalid_forwarding_telemetry(
                "single_hop_xff",
                SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            )
        else:
            client = _select_client_from_chain(xff_chain, trusted_hops=trusted_hops)
            if client is not None:
                cf_raw = request.headers.get("cf-connecting-ip", "").strip()
                if cf_raw:
                    cf_client = normalize_client_source(cf_raw)
                    if cf_client is not None and cf_client != client:
                        _record_invalid_forwarding_telemetry(
                            "cf_connecting_ip_mismatch",
                            SourceResolutionPath.MALFORMED_FORWARDING,
                        )
                        return ClientSourceResolution(
                            source=peer,
                            path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
                            invalid_forwarding=True,
                        )
                return ClientSourceResolution(
                    source=client,
                    path=SourceResolutionPath.FORWARDED_CHAIN,
                )

    cf_raw = request.headers.get("cf-connecting-ip", "").strip()
    if cf_raw and xff_chain and len(xff_chain) >= 2:
        cf_client = normalize_client_source(cf_raw)
        rightmost = xff_chain[-1]
        if cf_client is not None and cf_client == rightmost:
            return ClientSourceResolution(
                source=cf_client,
                path=SourceResolutionPath.CF_CONNECTING_IP,
            )
        if cf_client is None:
            _record_invalid_forwarding_telemetry(
                "malformed_cf_connecting_ip",
                SourceResolutionPath.MALFORMED_FORWARDING,
            )

    if not xff_raw:
        forwarded_raw = request.headers.get("forwarded", "").strip()
        if forwarded_raw:
            forwarded_chain = _parse_forwarded_header(forwarded_raw)
            if forwarded_chain is None:
                _record_invalid_forwarding_telemetry(
                    "malformed_forwarded",
                    SourceResolutionPath.MALFORMED_FORWARDING,
                )
                return ClientSourceResolution(
                    source=peer,
                    path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
                    invalid_forwarding=True,
                )
            if len(forwarded_chain) >= 2:
                client = _select_client_from_chain(forwarded_chain, trusted_hops=trusted_hops)
                if client is not None:
                    return ClientSourceResolution(
                        source=client,
                        path=SourceResolutionPath.FORWARDED_RFC7239,
                    )

    if _has_forwarding_headers(request):
        _record_invalid_forwarding_telemetry(
            "unresolvable_forwarding_chain",
            SourceResolutionPath.TRUSTED_PEER_FALLBACK,
        )
    return ClientSourceResolution(
        source=peer,
        path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
        invalid_forwarding=_has_forwarding_headers(request),
    )


def client_source_trust_status(settings: Settings) -> dict[str, bool]:
    """Non-sensitive deployment verification for /health."""
    return {
        "proxy_cidrs_configured": bool(settings.admin_trusted_proxy_cidrs),
        "edge_cidrs_configured": bool(settings.admin_trusted_edge_cidrs),
    }
