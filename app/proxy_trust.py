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
from uvicorn.middleware.proxy_headers import _TrustedHosts

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDED_LOG_INTERVAL_SECONDS = 60.0
_last_invalid_forwarded_log_at = 0.0

_FORWARDED_FOR_PARAM = re.compile(
    r"""for=(?:"\[([^\]]+)\]"|\"([^\"]+)\"|([^;,\s]+))""",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_FORWARDED = "trusted_forwarded"
    UNKNOWN_PEER = "unknown_peer"
    INVALID_FORWARDED = "invalid_forwarded"
    UNTRUSTED_FORWARDED = "untrusted_forwarded"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: SourceResolutionPath


def reset_proxy_trust_telemetry() -> None:
    """Clear sampled forwarding telemetry counters (tests only)."""
    global _last_invalid_forwarded_log_at
    _last_invalid_forwarded_log_at = 0.0


def trusted_proxy_boundary(spec: str) -> _TrustedHosts:
    """Build the same trusted-host boundary Uvicorn uses for forwarded headers."""
    cleaned = spec.strip()
    if not cleaned:
        return _TrustedHosts([])
    return _TrustedHosts(cleaned)


def boundary_configured(boundary: _TrustedHosts) -> bool:
    """Return whether any trusted proxy addresses are configured."""
    return bool(
        boundary.always_trust
        or boundary.trusted_literals
        or boundary.trusted_hosts
        or boundary.trusted_networks
    )


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > 253:
        return None
    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            return None
        candidate = candidate[1:bracket_end]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _parse_forwarded_header(value: str) -> list[str]:
    addresses: list[str] = []
    for entry in value.split(","):
        match = _FORWARDED_FOR_PARAM.search(entry)
        if match is None:
            continue
        raw = next(group for group in match.groups() if group is not None)
        if raw.casefold() == "_hidden":
            continue
        if raw.startswith("["):
            bracket_end = raw.find("]")
            if bracket_end != -1:
                addresses.append(raw[1:bracket_end])
                continue
        if raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
            addresses.append(raw.rsplit(":", 1)[0])
            continue
        addresses.append(raw)
    return addresses


def _chain_too_long(chain: list[str]) -> bool:
    return len(chain) > MAX_FORWARDED_CHAIN_LENGTH


def _resolve_from_chain(
    chain: list[str],
    boundary: _TrustedHosts,
) -> tuple[str | None, SourceResolutionPath]:
    if not chain:
        return None, SourceResolutionPath.INVALID_FORWARDED
    if _chain_too_long(chain):
        return None, SourceResolutionPath.INVALID_FORWARDED

    joined = ", ".join(chain)
    host, _port = boundary.get_trusted_client_address(joined)
    normalized = normalize_client_address(host)
    if normalized is None:
        return None, SourceResolutionPath.INVALID_FORWARDED
    return normalized, SourceResolutionPath.TRUSTED_FORWARDED


def _peer_source(peer: str | None) -> str:
    normalized = normalize_client_address(peer)
    if normalized is not None:
        return normalized
    if peer:
        literal = peer.strip()
        if literal and len(literal) <= 253:
            return literal
    return "unknown"


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _log_untrusted_forwarding_attempt(path: SourceResolutionPath) -> None:
    global _last_invalid_forwarded_log_at
    now = time.monotonic()
    if now - _last_invalid_forwarded_log_at < _INVALID_FORWARDED_LOG_INTERVAL_SECONDS:
        return
    _last_invalid_forwarded_log_at = now
    _logger.warning(
        "Ignored untrusted or invalid admin login forwarding headers",
        extra={"source_resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Production chain: public client → Cloudflare → Render load balancer → Uvicorn.

    Forwarding headers are parsed right-to-left against the configured trusted-proxy
    boundary (matching Uvicorn ``--forwarded-allow-ips``). Headers are consulted only
    when the immediate TCP peer is a verified member of that boundary. Uvicorn may
    already rewrite ``request.client`` to the resolved address; in that case the peer
    is not trusted and the framework-resolved address is used without re-reading raw
    header chains.

    Header precedence when the immediate peer is trusted:

    1. ``X-Forwarded-For``
    2. RFC 7239 ``Forwarded`` (``for=`` entries)
    3. Immediate peer address

    ``CF-Connecting-IP`` and other vendor-specific headers are intentionally ignored
    so direct access to the public Render origin cannot spoof a Cloudflare-derived
    client address.
    """
    boundary = trusted_proxy_boundary(settings.admin_trusted_proxy_cidrs)
    peer = _immediate_peer_host(request)
    normalized_peer = _peer_source(peer)

    if not boundary_configured(boundary):
        if peer is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution(
            normalized_peer,
            SourceResolutionPath.DIRECT_PEER,
        )

    if peer is None or peer not in boundary:
        has_forwarding_headers = any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        )
        if has_forwarding_headers:
            _log_untrusted_forwarding_attempt(SourceResolutionPath.UNTRUSTED_FORWARDED)
        if peer is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution(
            normalized_peer,
            SourceResolutionPath.DIRECT_PEER,
        )

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        chain = [item.strip() for item in x_forwarded_for.split(",") if item.strip()]
        resolved, path = _resolve_from_chain(chain, boundary)
        if resolved is not None:
            return ClientSourceResolution(resolved, path)
        _log_untrusted_forwarding_attempt(path)
        if peer is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution(normalized_peer, SourceResolutionPath.DIRECT_PEER)

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        chain = _parse_forwarded_header(forwarded)
        resolved, path = _resolve_from_chain(chain, boundary)
        if resolved is not None:
            return ClientSourceResolution(resolved, path)
        _log_untrusted_forwarding_attempt(path)
        if peer is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution(normalized_peer, SourceResolutionPath.DIRECT_PEER)

    if peer is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)
    return ClientSourceResolution(normalized_peer, SourceResolutionPath.DIRECT_PEER)
