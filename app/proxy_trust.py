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

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32
_UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS = 60.0

# Bounded telemetry paths (no raw addresses).
PATH_DIRECT_PEER = "direct_peer"
PATH_XFF_TRUSTED_WALK = "xff_trusted_walk"
PATH_FORWARDED_RFC7239 = "forwarded_rfc7239"
PATH_CF_CONNECTING_IP_VERIFIED = "cf_connecting_ip_verified"
PATH_UNTRUSTED_HEADERS_IGNORED = "untrusted_headers_ignored"
PATH_MALFORMED_CHAIN = "malformed_chain"
PATH_UNKNOWN_PEER = "unknown_peer"
PATH_UNKNOWN_CLIENT = "unknown_client"

_untrusted_header_log_lock = Lock()
_untrusted_header_log_last_monotonic = 0.0

_FORWARDED_FOR_TOKEN = re.compile(r"^for=(?P<value>[^;]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str
    untrusted_headers_seen: bool = False


class TrustedProxyBoundary:
    """Configured trusted proxy networks for hop verification."""

    def __init__(self, cidrs: Iterable[str]) -> None:
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
            self._parse_network(entry) for entry in cidrs if entry.strip()
        )

    @staticmethod
    def _parse_network(raw: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        value = raw.strip()
        if "/" not in value:
            parsed = ipaddress.ip_address(value)
            suffix = 32 if parsed.version == 4 else 128
            return ipaddress.ip_network(f"{value}/{suffix}", strict=False)
        return ipaddress.ip_network(value, strict=False)

    def contains(self, address: str) -> bool:
        normalized = normalize_ip_address(address)
        if normalized is None:
            return False
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        return any(parsed in network for network in self._networks)

    @property
    def configured(self) -> bool:
        return bool(self._networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; return None when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, port = candidate[1:].partition("]")
        if port.startswith(":"):
            candidate = host
        else:
            candidate = host

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.rpartition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if parsed.version == 4:
        return str(parsed)
    return parsed.compressed


def parse_x_forwarded_for_chain(header_value: str) -> list[str]:
    """Split an X-Forwarded-For header into normalized hop strings."""
    if len(header_value) > 2048:
        return []
    hops: list[str] = []
    for part in header_value.split(","):
        normalized = normalize_ip_address(part)
        if normalized is None:
            return []
        hops.append(normalized)
        if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
            return []
    return hops


def parse_forwarded_header(header_value: str) -> list[str]:
    """Extract client addresses from an RFC 7239 Forwarded header."""
    if len(header_value) > 4096:
        return []
    hops: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN.search(entry.strip())
        if match is None:
            continue
        value = match.group("value").strip().strip('"')
        if value.lower() == "unknown":
            continue
        normalized = normalize_ip_address(value)
        if normalized is None:
            return []
        hops.append(normalized)
        if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
            return []
    return hops


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    return normalize_ip_address(request.client.host)


def _is_trusted_hop(
    address: str,
    *,
    trusted: TrustedProxyBoundary,
    cloudflare: TrustedProxyBoundary,
) -> bool:
    return trusted.contains(address) or cloudflare.contains(address)


def _trusted_walk_client(
    chain: list[str],
    *,
    trusted: TrustedProxyBoundary,
    cloudflare: TrustedProxyBoundary,
) -> str | None:
    if not chain:
        return None
    remaining = list(chain)
    while remaining and _is_trusted_hop(
        remaining[-1],
        trusted=trusted,
        cloudflare=cloudflare,
    ):
        remaining.pop()
    if not remaining:
        return None
    if len(remaining) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return remaining[-1]


def _forwarding_headers_present(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _log_untrusted_headers(path: str) -> None:
    global _untrusted_header_log_last_monotonic
    now = time.monotonic()
    with _untrusted_header_log_lock:
        if now - _untrusted_header_log_last_monotonic < _UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS:
            return
        _untrusted_header_log_last_monotonic = now
    _logger.info(
        "Admin login source resolution ignored untrusted forwarding headers",
        extra={"source_resolution_path": path},
    )


def _log_resolution(resolution: ClientSourceResolution) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Production chain: browser → Cloudflare → Render load balancer → Uvicorn.

    Forwarding headers are honored only when the immediate TCP peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. The client address is derived by walking the
    forwarding chain from the right and stripping trusted proxy hops. Vendor
    headers such as ``CF-Connecting-IP`` are accepted only after a verified
    Cloudflare hop appears in the trusted chain.
    """
    trusted = TrustedProxyBoundary(settings.admin_trusted_proxy_cidrs)
    cloudflare = TrustedProxyBoundary(settings.admin_trusted_cloudflare_cidrs)
    peer = _immediate_peer(request)
    headers_present = _forwarding_headers_present(request)

    if peer is None:
        resolution = ClientSourceResolution("unknown", PATH_UNKNOWN_PEER)
        _log_resolution(resolution)
        return resolution

    if not trusted.configured or not trusted.contains(peer):
        path = PATH_UNTRUSTED_HEADERS_IGNORED if headers_present else PATH_DIRECT_PEER
        if headers_present:
            _log_untrusted_headers(path)
        resolution = ClientSourceResolution(peer, path, untrusted_headers_seen=headers_present)
        _log_resolution(resolution)
        return resolution

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_chain = parse_x_forwarded_for_chain(xff_header) if xff_header else []
    chain = xff_chain + [peer]
    if xff_header and not xff_chain:
        resolution = ClientSourceResolution("unknown", PATH_MALFORMED_CHAIN)
        _log_resolution(resolution)
        return resolution

    client = _trusted_walk_client(chain, trusted=trusted, cloudflare=cloudflare)
    if client is not None:
        resolution = ClientSourceResolution(client, PATH_XFF_TRUSTED_WALK)
        _log_resolution(resolution)
        return resolution

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_chain = parse_forwarded_header(forwarded_header)
        if not forwarded_chain:
            resolution = ClientSourceResolution("unknown", PATH_MALFORMED_CHAIN)
            _log_resolution(resolution)
            return resolution
        forwarded_walk = _trusted_walk_client(
            forwarded_chain + [peer],
            trusted=trusted,
            cloudflare=cloudflare,
        )
        if forwarded_walk is not None:
            resolution = ClientSourceResolution(forwarded_walk, PATH_FORWARDED_RFC7239)
            _log_resolution(resolution)
            return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and cloudflare.configured and any(
        cloudflare.contains(hop) for hop in chain
    ):
        cf_client = normalize_ip_address(cf_header)
        if cf_client is not None:
            resolution = ClientSourceResolution(cf_client, PATH_CF_CONNECTING_IP_VERIFIED)
            _log_resolution(resolution)
            return resolution

    resolution = ClientSourceResolution("unknown", PATH_UNKNOWN_CLIENT)
    _log_resolution(resolution)
    return resolution
