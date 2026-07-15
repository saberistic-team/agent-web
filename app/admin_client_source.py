"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

MAX_FORWARD_CHAIN_LENGTH = 10
MAX_FORWARD_HEADER_LENGTH = 2048

# Render private-network peers and loopback (production default).
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

_logger = logging.getLogger(__name__)
_invalid_forward_last_logged = 0.0
_INVALID_FORWARD_LOG_INTERVAL_SECONDS = 60.0
_INVALID_FORWARD_SAMPLE_RATE = 0.05


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    CLOUDFLARE_CONNECTING_IP = "cloudflare_connecting_ip"
    TRUSTED_PEER_NO_FORWARDING = "trusted_peer_no_forwarding"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: SourceResolutionPath


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return parsed trusted-proxy networks from settings."""
    cidrs = settings.admin_trusted_proxy_cidrs
    if not cidrs and settings.admin_trust_proxy_headers:
        cidrs = DEFAULT_TRUSTED_PROXY_CIDRS
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        try:
            networks.append(ipaddress.ip_network(raw.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def proxy_trust_enabled(settings: Settings) -> bool:
    """True when forwarded identity may be considered for trusted immediate peers."""
    return bool(settings.admin_trusted_proxy_cidrs or settings.admin_trust_proxy_headers)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; return None when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host = candidate[1 : candidate.index("]")]
        if "]:" in candidate:
            port_part = candidate.split("]:", 1)[1]
            if not port_part.isdigit():
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if not port.isdigit():
            return None
    else:
        host = candidate

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


def immediate_peer_host(request: Request) -> str | None:
    """Return the transport peer host before any application-level header trust."""
    if request.client is None:
        return None
    raw = request.client.host.strip()
    if not raw:
        return None
    normalized = normalize_ip_address(raw)
    if normalized is not None:
        return normalized
    # Test clients and non-IP transports (e.g. UNIX sockets) keep a stable peer label.
    return raw.lower()


def _is_trusted_address(
    host: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks)


def _parse_forwarded_for_chain(header_value: str) -> list[str] | None:
    if not header_value.strip():
        return []
    if len(header_value) > MAX_FORWARD_HEADER_LENGTH:
        return None
    normalized: list[str] = []
    for element in header_value.split(","):
        parsed = normalize_ip_address(element)
        if parsed is None:
            return None
        normalized.append(parsed)
    if len(normalized) > MAX_FORWARD_CHAIN_LENGTH:
        return None
    return normalized


_FORWARDED_FOR_TOKEN = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"\[?([^;"\s]+)\]?|([^;,\s]+))',
    re.IGNORECASE,
)


def _parse_forwarded_header(header_value: str) -> list[str] | None:
    if len(header_value) > MAX_FORWARD_HEADER_LENGTH:
        return None
    normalized: list[str] = []
    for match in _FORWARDED_FOR_TOKEN.finditer(header_value):
        raw_host = match.group(1) or match.group(2) or match.group(3) or ""
        parsed = normalize_ip_address(raw_host)
        if parsed is None:
            return None
        normalized.append(parsed)
    if not normalized or len(normalized) > MAX_FORWARD_CHAIN_LENGTH:
        return None
    return normalized


def _cloudflare_path_indicated(request: Request) -> bool:
    """True when standard Cloudflare edge headers indicate the request transited CF."""
    if request.headers.get("cf-ray", "").strip():
        return True
    if request.headers.get("cf-visitor", "").strip():
        return True
    return False


def _resolve_from_forward_chain(
    chain: list[str],
    *,
    immediate_peer: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Strip trusted suffix hops; the leftmost remaining hop is the client."""
    working = list(chain)
    while working and (
        working[-1] == immediate_peer
        or _is_trusted_address(working[-1], trusted_networks)
    ):
        working.pop()
    if not working:
        return immediate_peer
    return working[0]


def _maybe_log_invalid_forwarding() -> None:
    global _invalid_forward_last_logged
    now = time.monotonic()
    if now - _invalid_forward_last_logged < _INVALID_FORWARD_LOG_INTERVAL_SECONDS:
        return
    if random.random() > _INVALID_FORWARD_SAMPLE_RATE:
        return
    _invalid_forward_last_logged = now
    _logger.warning(
        "Ignored untrusted or malformed admin login forwarding headers",
        extra={"source_resolution_path": SourceResolutionPath.INVALID_FORWARDING.value},
    )


def reset_client_source_telemetry_for_tests() -> None:
    """Reset sampled invalid-forwarding telemetry (tests only)."""
    global _invalid_forward_last_logged
    _invalid_forward_last_logged = 0.0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source through a verified proxy boundary."""
    peer = immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.DIRECT_PEER)

    trusted_networks = trusted_proxy_networks(settings)
    if not proxy_trust_enabled(settings) or not trusted_networks:
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    if not _is_trusted_address(peer, trusted_networks):
        if _forwarding_headers_present(request):
            _maybe_log_invalid_forwarding()
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    if _cloudflare_path_indicated(request):
        cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_connecting_ip:
            normalized = normalize_ip_address(cf_connecting_ip)
            if normalized is not None:
                return ClientSourceResolution(
                    normalized,
                    SourceResolutionPath.CLOUDFLARE_CONNECTING_IP,
                )
            _maybe_log_invalid_forwarding()
            return ClientSourceResolution(peer, SourceResolutionPath.INVALID_FORWARDING)

    xff_chain = _parse_forwarded_for_chain(request.headers.get("x-forwarded-for", ""))
    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))

    if xff_chain is not None and xff_chain:
        resolved = _resolve_from_forward_chain(
            xff_chain,
            immediate_peer=peer,
            trusted_networks=trusted_networks,
        )
        if resolved is not None:
            return ClientSourceResolution(resolved, SourceResolutionPath.TRUSTED_XFF_CHAIN)

    if forwarded_chain is not None and forwarded_chain:
        resolved = _resolve_from_forward_chain(
            forwarded_chain,
            immediate_peer=peer,
            trusted_networks=trusted_networks,
        )
        if resolved is not None:
            return ClientSourceResolution(resolved, SourceResolutionPath.TRUSTED_XFF_CHAIN)

    if xff_chain is None or (request.headers.get("forwarded", "").strip() and forwarded_chain is None):
        if _forwarding_headers_present(request):
            _maybe_log_invalid_forwarding()
        return ClientSourceResolution(peer, SourceResolutionPath.INVALID_FORWARDING)

    return ClientSourceResolution(peer, SourceResolutionPath.TRUSTED_PEER_NO_FORWARDING)


def _forwarding_headers_present(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for", "").strip()
        or request.headers.get("forwarded", "").strip()
        or request.headers.get("cf-connecting-ip", "").strip()
    )


def log_client_source_resolution(resolution: ClientSourceResolution) -> None:
    """Emit bounded structured telemetry without raw addresses or header chains."""
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path.value},
    )
