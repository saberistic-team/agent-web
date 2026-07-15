"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative cap on forwarded-hop parsing; excess hops fail closed.
_MAX_FORWARDED_HOPS = 32

# Sample at most one invalid/untrusted forwarding telemetry event per interval.
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0

_telemetry_lock = Lock()
_last_invalid_telemetry_at = 0.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    CONSERVATIVE_FALLBACK = "conservative_fallback"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: SourceResolutionPath
    invalid_forwarding: bool = False

def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; return None when invalid."""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > 128:
        return None

    # Bracketed IPv6 with optional port: [::1]:8080
    bracket_match = re.fullmatch(r"\[([^\]]+)\](?::\d+)?", candidate)
    if bracket_match:
        candidate = bracket_match.group(1)

    # IPv4/hostname with trailing port: 203.0.113.1:443
    if candidate.count(":") == 1 and "." in candidate:
        host, maybe_port = candidate.rsplit(":", 1)
        if maybe_port.isdigit():
            candidate = host

    # Strip zone id for IPv6: fe80::1%eth0
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _ip_in_trusted_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _parse_x_forwarded_for(header_value: str) -> tuple[str, ...] | None:
    if not header_value or len(header_value) > 2048:
        return None
    hops: list[str] = []
    for part in header_value.split(","):
        hop = part.strip()
        if not hop:
            continue
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        hops.append(normalized)
        if len(hops) > _MAX_FORWARDED_HOPS:
            return None
    return tuple(hops)


def _parse_forwarded_header(header_value: str) -> str | None:
    """Extract the first ``for="..."`` client from an RFC 7239 Forwarded header."""
    if not header_value or len(header_value) > 2048:
        return None
    for entry in header_value.split(","):
        for token in entry.split(";"):
            token = token.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip().strip('"')
            if value.lower() == "unknown":
                continue
            if value.startswith("["):
                end = value.find("]")
                if end == -1:
                    return None
                value = value[1:end]
            else:
                value = value.split(":", 1)[0]
            normalized = normalize_client_address(value)
            if normalized is None:
                return None
            return normalized
    return None


def _resolve_from_trusted_xff(
    xff_hops: tuple[str, ...],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk X-Forwarded-For right-to-left, skipping trusted proxy hops."""
    for hop in reversed(xff_hops):
        if not _ip_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _record_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    if resolution.invalid_forwarding:
        global _last_invalid_telemetry_at
        now = time.monotonic()
        with _telemetry_lock:
            if now - _last_invalid_telemetry_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
                return
            _last_invalid_telemetry_at = now
        _logger.info(
            "Admin login source forwarding rejected",
            extra={"resolution_path": resolution.path.value},
        )
        return

    _logger.debug(
        "Admin login source resolved",
        extra={"resolution_path": resolution.path.value},
    )


def reset_admin_client_source_telemetry() -> None:
    """Reset sampled invalid-forwarding telemetry (tests only)."""
    global _last_invalid_telemetry_at
    with _telemetry_lock:
        _last_invalid_telemetry_at = 0.0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Trust model (production: Client → Cloudflare → Render load balancer → Uvicorn):

    1. Determine the immediate TCP peer (``request.client.host``).
    2. When the peer is **not** in ``ADMIN_TRUSTED_PROXY_CIDRS``, ignore all
       forwarding and vendor headers; use the peer address only.
    3. When the peer **is** trusted, derive the client using this precedence:

       a. Right-to-left ``X-Forwarded-For`` walk (Cloudflare appends the
          connecting address; leftmost values may be attacker-controlled).
       b. RFC 7239 ``Forwarded`` header ``for=`` value when XFF is absent.
       c. ``CF-Connecting-IP`` only when it agrees with the XFF-derived client.
       d. Conservative ``unknown`` when the chain is empty, malformed, or only
          contains trusted proxy hops.

    Uvicorn's ``--forwarded-allow-ips`` is configured to avoid rewriting
    ``request.client`` from leftmost XFF; this resolver owns the trust boundary.
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    peer_raw = request.client.host if request.client is not None else ""
    peer = normalize_client_address(peer_raw)

    if peer is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.CONSERVATIVE_FALLBACK,
            invalid_forwarding=bool(peer_raw.strip()),
        )
        _record_resolution_telemetry(resolution)
        return resolution

    if not trusted_networks or not _ip_in_trusted_networks(peer, trusted_networks):
        resolution = ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.DIRECT_PEER,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_hops = _parse_x_forwarded_for(xff_header) if xff_header else None
    if xff_header and xff_hops is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.INVALID_FORWARDING,
            invalid_forwarding=True,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    xff_client: str | None = None
    if xff_hops:
        xff_client = _resolve_from_trusted_xff(xff_hops, trusted_networks)
        if xff_client is not None:
            resolution = ClientSourceResolution(
                source=xff_client,
                path=SourceResolutionPath.TRUSTED_XFF,
            )
            _record_resolution_telemetry(resolution)
            return resolution

    forwarded_header = request.headers.get("forwarded", "")
    forwarded_client = (
        _parse_forwarded_header(forwarded_header) if forwarded_header else None
    )
    if forwarded_header and forwarded_client is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.INVALID_FORWARDING,
            invalid_forwarding=True,
        )
        _record_resolution_telemetry(resolution)
        return resolution
    if forwarded_client is not None:
        resolution = ClientSourceResolution(
            source=forwarded_client,
            path=SourceResolutionPath.TRUSTED_FORWARDED,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_client = normalize_client_address(cf_header) if cf_header else None
    if cf_header and cf_client is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.INVALID_FORWARDING,
            invalid_forwarding=True,
        )
        _record_resolution_telemetry(resolution)
        return resolution

    if cf_client is not None and xff_hops:
        xff_agreement = _resolve_from_trusted_xff(xff_hops, trusted_networks)
        # When every XFF hop is trusted, agreement is None; do not trust CF alone.
        if xff_agreement is not None and cf_client == xff_agreement:
            resolution = ClientSourceResolution(
                source=cf_client,
                path=SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
            )
            _record_resolution_telemetry(resolution)
            return resolution

    resolution = ClientSourceResolution(
        source="unknown",
        path=SourceResolutionPath.CONSERVATIVE_FALLBACK,
        invalid_forwarding=bool(xff_header or forwarded_header or cf_header),
    )
    _record_resolution_telemetry(resolution)
    return resolution
