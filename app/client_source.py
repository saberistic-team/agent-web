"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

MAX_FORWARD_CHAIN_ENTRIES = 32
MAX_HEADER_VALUE_LENGTH = 2048
_UNTRUSTED_SAMPLE_INTERVAL = 100

# Render internal load-balancer ranges used in production (see render.yaml).
DEFAULT_TRUSTED_PROXY_IPS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

_FORWARDED_FOR_TOKEN_RE = re.compile(
    r'for=(?:"?\[([^\]]+)\]"?|"?([^";,\s]+)"?)',
    re.IGNORECASE,
)

_logger = logging.getLogger(__name__)
_untrusted_forwarding_attempt_counter = 0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    UNKNOWN_PEER = "unknown_peer"
    XFF_TRUSTED_HOP = "xff_trusted_hop"
    FORWARDED_TRUSTED_HOP = "forwarded_trusted_hop"
    CF_CONNECTING_IP_VERIFIED = "cf_connecting_ip_verified"
    CONSERVATIVE_PEER = "conservative_peer"
    CONSERVATIVE_UNKNOWN = "conservative_unknown"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting forwarding headers."""

    source: str
    path: SourceResolutionPath


class TrustedProxyBoundary:
    """Configured proxy boundary used for peer verification and hop skipping."""

    def __init__(self, entries: tuple[str, ...]) -> None:
        self._networks: tuple[
            ipaddress.IPv4Network | ipaddress.IPv6Network,
            ...,
        ] = tuple(self._parse_entry(entry) for entry in entries)

    @staticmethod
    def _parse_entry(
        entry: str,
    ) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        value = entry.strip()
        if not value:
            raise ValueError("empty trusted proxy entry")
        if "/" not in value:
            address = ipaddress.ip_address(value)
            prefix = 32 if address.version == 4 else 128
            return ipaddress.ip_network(f"{value}/{prefix}", strict=False)
        return ipaddress.ip_network(value, strict=False)

    def contains(self, address: str) -> bool:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in self._networks)


@lru_cache(maxsize=8)
def trusted_proxy_boundary(entries: tuple[str, ...]) -> TrustedProxyBoundary:
    return TrustedProxyBoundary(entries)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) or return None for invalid input."""
    value = raw.strip()
    if not value:
        return None

    if value.startswith("[") and "]" in value:
        bracket_end = value.index("]")
        host = value[1:bracket_end]
        remainder = value[bracket_end + 1 :]
        if remainder.startswith(":") and remainder[1:].isdigit():
            value = host
        else:
            value = host
    elif value.count(":") == 1:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit() and "." in host:
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


def _parse_forwarded_for(raw: str) -> list[str]:
    if not raw or len(raw) > MAX_HEADER_VALUE_LENGTH:
        return []
    parts = [part.strip() for part in raw.split(",")]
    if not parts or len(parts) > MAX_FORWARD_CHAIN_ENTRIES:
        return []
    if any(not part for part in parts):
        return []
    return parts


def _parse_forwarded_header(raw: str) -> list[str]:
    if not raw or len(raw) > MAX_HEADER_VALUE_LENGTH:
        return []
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not entries or len(entries) > MAX_FORWARD_CHAIN_ENTRIES:
        return []
    hops: list[str] = []
    for entry in entries:
        match = _FORWARDED_FOR_TOKEN_RE.search(entry)
        if match is None:
            return []
        token = match.group(1) or match.group(2) or ""
        if not token or token.casefold() == "unknown":
            return []
        hops.append(token.strip())
    return hops


def _chain_aligns_with_peer(
    hops: list[str],
    peer: str,
    boundary: TrustedProxyBoundary,
) -> bool:
    if not hops:
        return False
    rightmost = hops[-1]
    return rightmost == peer or boundary.contains(rightmost)


def _resolve_from_trusted_chain(
    hops: list[str],
    *,
    peer: str,
    boundary: TrustedProxyBoundary,
) -> str | None:
    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    if not _chain_aligns_with_peer(normalized_hops, peer, boundary):
        return None

    for hop in reversed(normalized_hops):
        if hop == peer or boundary.contains(hop):
            continue
        return hop
    return None


def _cf_connecting_ip_verified(
    request: Request,
    *,
    xff_hops: list[str],
    peer: str,
    boundary: TrustedProxyBoundary,
) -> str | None:
    raw = request.headers.get("cf-connecting-ip", "").strip()
    if not raw or len(raw) > MAX_HEADER_VALUE_LENGTH:
        return None

    cf_ip = normalize_ip_address(raw)
    if cf_ip is None:
        return None

    normalized_hops: list[str] = []
    for hop in xff_hops:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    if len(normalized_hops) < 2:
        return None
    if not _chain_aligns_with_peer(normalized_hops, peer, boundary):
        return None

    for hop in normalized_hops[1:]:
        if hop != peer and not boundary.contains(hop):
            return None

    leftmost = normalized_hops[0]
    if boundary.contains(leftmost):
        return None
    if cf_ip != leftmost:
        return None
    return cf_ip


def _record_untrusted_forwarding(reason: str) -> None:
    global _untrusted_forwarding_attempt_counter
    _untrusted_forwarding_attempt_counter += 1
    if _untrusted_forwarding_attempt_counter % _UNTRUSTED_SAMPLE_INTERVAL != 0:
        return
    _logger.info(
        "Admin login source: sampled untrusted forwarding attempt",
        extra={
            "source_resolution_reason": reason,
            "source_resolution_path": SourceResolutionPath.CONSERVATIVE_PEER.value,
        },
    )


def reset_untrusted_forwarding_telemetry() -> None:
    """Clear sampled untrusted-forwarding counter (tests only)."""
    global _untrusted_forwarding_attempt_counter
    _untrusted_forwarding_attempt_counter = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login limiter source for ``request``.

    Production chain: public client → Cloudflare edge → Render load balancer →
    Uvicorn. Forwarding headers are consulted only when the immediate peer is a
    member of ``ADMIN_TRUSTED_PROXY_IPS``. Client identity is derived by walking
    ``X-Forwarded-For`` (or ``Forwarded``) from the trusted peer right-to-left
    and selecting the first non-trusted hop. Vendor headers such as
    ``CF-Connecting-IP`` are accepted only when the trusted chain corroborates
    them.
    """
    peer_raw = request.client.host if request.client is not None else None
    if peer_raw is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)

    peer = normalize_ip_address(peer_raw)
    if peer is None:
        opaque_peer = peer_raw.strip().lower() or "unknown"
        return ClientSourceResolution(opaque_peer, SourceResolutionPath.DIRECT_PEER)

    trusted_entries = settings.admin_trusted_proxy_ips
    if not trusted_entries:
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    boundary = trusted_proxy_boundary(trusted_entries)
    if not boundary.contains(peer):
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    xff_hops = _parse_forwarded_for(request.headers.get("x-forwarded-for", ""))
    client = _resolve_from_trusted_chain(xff_hops, peer=peer, boundary=boundary)
    if client is not None:
        return ClientSourceResolution(client, SourceResolutionPath.XFF_TRUSTED_HOP)

    forwarded_hops = _parse_forwarded_header(request.headers.get("forwarded", ""))
    if forwarded_hops:
        client = _resolve_from_trusted_chain(
            forwarded_hops,
            peer=peer,
            boundary=boundary,
        )
        if client is not None:
            return ClientSourceResolution(
                client,
                SourceResolutionPath.FORWARDED_TRUSTED_HOP,
            )

    cf_client = _cf_connecting_ip_verified(
        request,
        xff_hops=xff_hops,
        peer=peer,
        boundary=boundary,
    )
    if cf_client is not None:
        return ClientSourceResolution(
            cf_client,
            SourceResolutionPath.CF_CONNECTING_IP_VERIFIED,
        )

    had_forwarding_headers = bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )
    if had_forwarding_headers:
        _record_untrusted_forwarding("untrusted_or_malformed_forwarding")
        return ClientSourceResolution(peer, SourceResolutionPath.CONSERVATIVE_PEER)

    return ClientSourceResolution(peer, SourceResolutionPath.CONSERVATIVE_PEER)
