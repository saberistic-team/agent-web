"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import Request

from app.network_utils import ip_in_trusted_networks, normalize_ip, parse_trusted_networks

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 10
_UNKNOWN_SOURCE = "unknown"
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips-v4/ / ips-v6/).
# Used only to strip documented Cloudflare hops from forwarding chains after the
# immediate peer has been verified against ADMIN_TRUSTED_PROXY_CIDRS.
_CLOUDFLARE_PUBLISHED_CIDRS = (
    "173.245.48.0/20,"
    "103.21.244.0/22,"
    "103.22.200.0/22,"
    "103.31.4.0/22,"
    "141.101.64.0/18,"
    "108.162.192.0/18,"
    "190.93.240.0/20,"
    "188.114.96.0/20,"
    "197.234.240.0/22,"
    "198.41.128.0/17,"
    "162.158.0.0/15,"
    "104.16.0.0/13,"
    "104.24.0.0/14,"
    "172.64.0.0/13,"
    "131.0.72.0/22,"
    "2400:cb00::/32,"
    "2606:4700::/32,"
    "2803:f800::/32,"
    "2405:b500::/32,"
    "2405:8100::/32,"
    "2a06:98c0::/29,"
    "2c0f:f248::/32"
)

# RFC 7239 Forwarded: for=203.0.113.1, for="203.0.113.1", for="[2001:db8::1]:443"
_FORWARDED_FOR_PATTERN = re.compile(
    r'for=(?:"\[([^\]]+)\](?::\d+)?"|"([^"]+)"|([^";,\s]+))',
    re.IGNORECASE,
)

_invalid_forwarding_log_at = 0.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_CF_CONNECTING = "trusted_cf_connecting"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"
    UNTRUSTED_PEER = "untrusted_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the resolution path used."""

    source: str
    path: SourceResolutionPath


def _split_forwarding_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _resolve_from_forwarding_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain:
        return None
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    hops = list(chain)
    while hops and ip_in_trusted_networks(hops[-1], trusted_networks):
        hops.pop()
    if not hops:
        return None

    candidate = normalize_ip(hops[-1])
    if candidate is None or ip_in_trusted_networks(candidate, trusted_networks):
        return None
    return candidate


def _parse_forwarded_header(header_value: str) -> list[str]:
    addresses: list[str] = []
    for segment in header_value.split(","):
        match = _FORWARDED_FOR_PATTERN.search(segment)
        if match is None:
            continue
        raw = match.group(1) or match.group(2) or match.group(3) or ""
        normalized = normalize_ip(raw)
        if normalized is not None:
            addresses.append(normalized)
    return addresses


def _forwarding_trusted_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks stripped while walking a forwarding chain right-to-left."""
    if settings.admin_trusted_proxy_networks:
        return settings.admin_trusted_proxy_networks + parse_trusted_networks(
            _CLOUDFLARE_PUBLISHED_CIDRS
        )
    return ()


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    return host or None


def _combined_header_values(request: Request, name: str) -> str:
    """Join every raw header instance sharing ``name`` (RFC 7230 field-line equivalence).

    ``Request.headers.get`` only returns the first occurrence; duplicate header
    lines (rather than one comma-joined value) must not silently drop later
    values, since that would let a spoofed first header line hide a genuine
    trailing hop from the trusted-chain walk.
    """
    needle = name.lower().encode("latin-1")
    values = [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", [])
        if header_name.lower() == needle
    ]
    return ",".join(values)


def _log_invalid_forwarding(path: SourceResolutionPath) -> None:
    global _invalid_forwarding_log_at
    now = time.monotonic()
    if now - _invalid_forwarding_log_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _invalid_forwarding_log_at = now
    _logger.info(
        "Admin login client source rejected forwarding headers",
        extra={"source_resolution_path": path.value},
    )


def reset_client_source_telemetry_for_tests() -> None:
    """Reset rate-limited invalid-forwarding telemetry (tests only)."""
    global _invalid_forwarding_log_at
    _invalid_forwarding_log_at = 0.0


def resolve_trusted_proxy_cidr_strings(settings: Settings) -> tuple[str, ...]:
    """Configured ``ADMIN_TRUSTED_PROXY_CIDRS`` entries as a tuple of strings.

    Used for deployment-verification metadata (``/health``) — never includes
    raw request data, only the operator-configured boundary.
    """
    return tuple(
        part.strip() for part in settings.admin_trusted_proxy_cidrs.split(",") if part.strip()
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of ``settings.admin_trusted_proxy_networks``. The rightmost untrusted hop in
    ``X-Forwarded-For`` / ``Forwarded`` is used so attacker-controlled leftmost
    values cannot rotate limiter buckets through Cloudflare's append behavior.

    Header precedence (trusted peer required for items 1–3):

    1. ``X-Forwarded-For``
    2. ``Forwarded`` (RFC 7239 ``for=``)
    3. ``CF-Connecting-IP`` (vendor header accepted only behind the trusted proxy
       boundary — never from a direct public origin connection)
    """
    peer = _immediate_peer(request)
    if peer is None:
        return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=SourceResolutionPath.MISSING_PEER)

    normalized_peer = normalize_ip(peer)
    if normalized_peer is None:
        stripped_peer = peer.strip()
        if not stripped_peer:
            return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=SourceResolutionPath.MISSING_PEER)
        normalized_peer = stripped_peer

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.DIRECT_PEER,
        )

    trusted_networks = settings.admin_trusted_proxy_networks
    if not trusted_networks:
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.UNTRUSTED_PEER,
        )

    if not ip_in_trusted_networks(normalized_peer, trusted_networks):
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.UNTRUSTED_PEER,
        )

    forwarding_trusted = _forwarding_trusted_networks(settings)

    xff_header = _combined_header_values(request, "x-forwarded-for")
    if xff_header:
        chain = _split_forwarding_chain(xff_header)
        resolved = _resolve_from_forwarding_chain(chain, forwarding_trusted)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_XFF,
            )
        _log_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=SourceResolutionPath.INVALID_FORWARDING,
        )

    forwarded_header = _combined_header_values(request, "forwarded")
    if forwarded_header:
        chain = _parse_forwarded_header(forwarded_header)
        resolved = _resolve_from_forwarding_chain(chain, forwarding_trusted)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_FORWARDED,
            )
        _log_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=SourceResolutionPath.INVALID_FORWARDING,
        )

    cf_connecting_ip = _combined_header_values(request, "cf-connecting-ip")
    if cf_connecting_ip:
        resolved = normalize_ip(cf_connecting_ip)
        if resolved is not None and not ip_in_trusted_networks(resolved, forwarding_trusted):
            return ClientSourceResolution(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_CF_CONNECTING,
            )
        _log_invalid_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=SourceResolutionPath.INVALID_FORWARDING,
        )

    return ClientSourceResolution(
        source=_UNKNOWN_SOURCE,
        path=SourceResolutionPath.INVALID_FORWARDING,
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Normalized effective client address string (see ``docs/ADMIN_AUTH.md``).

    Convenience wrapper around :func:`resolve_admin_login_client_source` for
    callers that only need the resolved address, not the telemetry path.
    """
    return resolve_admin_login_client_source(request, settings).source
