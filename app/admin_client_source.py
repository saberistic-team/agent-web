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

# Conservative bound on comma-separated forwarding chains.
_MAX_FORWARDED_CHAIN_LENGTH = 32

# Sample at most one untrusted-forwarding log per interval per process.
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_last_untrusted_forwarding_log_at = 0.0


class SourceResolutionPath(StrEnum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    UNKNOWN_PEER = "unknown_peer"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    MALFORMED_FORWARDED = "malformed_forwarded"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source and the path used to derive it."""

    source: str
    path: SourceResolutionPath
    untrusted_forwarding_detected: bool = False


def parse_trusted_proxy_entries(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Parse comma-separated trusted proxy IPs and CIDR blocks."""
    entries: list[
        ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address
    ] = []
    for item in raw.split(","):
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


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 host strings deterministically.

    Strips whitespace, optional ``host:port`` / ``[ipv6]:port`` wrappers, and
    returns canonical compressed IPv6 or dotted IPv4. IPv4-mapped IPv6 is
    reduced to dotted IPv4.
    """
    if not raw:
        return None
    host = raw.strip()
    if not host:
        return None
    if len(host) > 253:
        return None

    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        address_part = host[1:end]
        remainder = host[end + 1 :]
        if remainder.startswith(":"):
            port = remainder[1:]
            if not port.isdigit():
                return None
        elif remainder:
            return None
        host = address_part
    elif host.count(":") == 1 and "." in host:
        address_part, port = host.rsplit(":", 1)
        if not port.isdigit():
            return None
        host = address_part

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def is_trusted_proxy_host(
    host: str | None,
    trusted_entries: Iterable[
        ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address
    ],
) -> bool:
    """Return whether ``host`` is a configured trusted proxy."""
    normalized = normalize_client_address(host or "")
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    for entry in trusted_entries:
        if isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            if address in entry:
                return True
        elif address == entry:
            return True
    return False


def _split_forwarded_chain(raw: str) -> list[str]:
    elements = [item.strip() for item in raw.split(",")]
    return [item for item in elements if item]


def _client_from_forwarded_chain(
    chain: list[str],
    trusted_entries: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address,
        ...,
    ],
) -> str | None:
    """Select the first untrusted hop walking right-to-left (proxy-append order)."""
    if not chain:
        return None
    if len(chain) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None

    for hop in reversed(chain):
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        if not is_trusted_proxy_host(normalized, trusted_entries):
            return normalized

    return normalize_client_address(chain[0])


_FORWARDED_FOR_RE = re.compile(r'for="?([^;,"]+)"?', re.IGNORECASE)


def _client_from_forwarded_header(
    raw: str,
    trusted_entries: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network | ipaddress.IPv4Address | ipaddress.IPv6Address,
        ...,
    ],
) -> str | None:
    """Parse RFC 7239 ``Forwarded`` header entries right-to-left."""
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    if not entries or len(entries) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None

    for entry in reversed(entries):
        match = _FORWARDED_FOR_RE.search(entry)
        if match is None:
            continue
        candidate = match.group(1).strip()
        if candidate.lower() == "unknown":
            continue
        normalized = normalize_client_address(candidate)
        if normalized is None:
            return None
        if not is_trusted_proxy_host(normalized, trusted_entries):
            return normalized
    return None


def _has_forwarding_headers(request: Request) -> bool:
    header_names = {name.decode("latin1").lower() for name, _ in request.scope.get("headers", [])}
    return bool(
        header_names.intersection(
            {
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "true-client-ip",
                "x-real-ip",
            }
        )
    )


def _log_untrusted_forwarding(path: SourceResolutionPath) -> None:
    global _last_untrusted_forwarding_log_at
    now = time.monotonic()
    if now - _last_untrusted_forwarding_log_at < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _last_untrusted_forwarding_log_at = now
    _logger.info(
        "Admin login source resolution ignored untrusted forwarding headers",
        extra={
            "source_resolution_path": path.value,
            "untrusted_forwarding_detected": True,
        },
    )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented): public client → Cloudflare edge → Render load
    balancer → Uvicorn. Forwarded identity is accepted only when the immediate
    TCP peer is a member of ``ADMIN_TRUSTED_PROXY_IPS``. Untrusted peers ignore
    ``X-Forwarded-For``, ``Forwarded``, ``CF-Connecting-IP``, and similar headers.

    Header precedence when the immediate peer is trusted:

    1. ``X-Forwarded-For`` — right-to-left, first untrusted hop (Cloudflare append-safe)
    2. ``Forwarded`` — RFC 7239 ``for=`` values, same right-to-left walk
    3. ``CF-Connecting-IP`` — only after (1) or (2) produced no client; never used
       for direct-origin requests where the peer is untrusted

    Uvicorn is started with matching ``--proxy-headers`` /
    ``--forwarded-allow-ips`` so ``request.client`` may already reflect the
    resolved public client when middleware ran. In that case the peer is not a
    trusted proxy and this function returns it without re-reading headers.
    """
    trusted_entries = settings.admin_trusted_proxy_entries
    peer_host = request.client.host if request.client is not None else None
    peer = normalize_client_address(peer_host or "")
    if peer is None and peer_host:
        peer = peer_host.strip().lower() or None

    if peer is None:
        if _has_forwarding_headers(request):
            _log_untrusted_forwarding(SourceResolutionPath.UNKNOWN_PEER)
        return ClientSourceResolution(
            source="unknown",
            path=SourceResolutionPath.UNKNOWN_PEER,
            untrusted_forwarding_detected=_has_forwarding_headers(request),
        )

    if not trusted_entries or not is_trusted_proxy_host(peer, trusted_entries):
        if _has_forwarding_headers(request):
            _log_untrusted_forwarding(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.DIRECT_PEER,
            untrusted_forwarding_detected=_has_forwarding_headers(request),
        )

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    if x_forwarded_for:
        chain = _split_forwarded_chain(x_forwarded_for)
        client = _client_from_forwarded_chain(chain, trusted_entries)
        if client is not None:
            return ClientSourceResolution(
                source=client,
                path=SourceResolutionPath.TRUSTED_FORWARDED,
            )
        _log_untrusted_forwarding(SourceResolutionPath.MALFORMED_FORWARDED)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.MALFORMED_FORWARDED,
            untrusted_forwarding_detected=True,
        )

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        client = _client_from_forwarded_header(forwarded, trusted_entries)
        if client is not None:
            return ClientSourceResolution(
                source=client,
                path=SourceResolutionPath.TRUSTED_FORWARDED,
            )
        _log_untrusted_forwarding(SourceResolutionPath.MALFORMED_FORWARDED)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.MALFORMED_FORWARDED,
            untrusted_forwarding_detected=True,
        )

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip:
        client = normalize_client_address(cf_connecting_ip)
        if client is not None and not is_trusted_proxy_host(client, trusted_entries):
            return ClientSourceResolution(
                source=client,
                path=SourceResolutionPath.TRUSTED_FORWARDED,
            )

    return ClientSourceResolution(
        source=peer,
        path=SourceResolutionPath.TRUSTED_PEER_FALLBACK,
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for limiter key material."""
    return resolve_admin_login_client_source(request, settings).source
