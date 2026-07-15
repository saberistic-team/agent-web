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

_logger = logging.getLogger(__name__)

# Conservative upper bound for comma-separated forwarding chains.
_MAX_FORWARDING_CHAIN_LENGTH = 32

# Sample at most one invalid forwarding attempt log per interval per process.
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0
_untrusted_telemetry_last_logged = 0.0

# RFC 7239 Forwarded: for=203.0.113.1 or for="[2001:db8::1]"
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[([^\]]+)\]"|"\s*([^";]+)\s*"|([^;,]+))',
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels; never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    CF_CONNECTING_IP = "cf_connecting_ip"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_HEADER = "forwarded_header"
    MALFORMED_FALLBACK = "malformed_fallback"
    MISSING_PEER = "missing_peer"
    ALL_TRUSTED_FALLBACK = "all_trusted_fallback"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and telemetry path."""

    source: str
    path: SourceResolutionPath


def parse_trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated proxy CIDRs/addresses; ignore invalid entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                addr = ipaddress.ip_address(token)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_literal(raw: str) -> str | None:
    """Normalize IPv4/IPv6 literals; map IPv4-mapped IPv6 to dotted quad."""
    candidate = raw.strip().strip('"').strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    # Strip bracketed or trailing :port for IPv4 (not raw IPv6 with single colon).
    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return addr.compressed


def _peer_identity(peer_host: str) -> str | None:
    """Return normalized peer material for direct-peer limiter buckets."""
    normalized = normalize_ip_literal(peer_host)
    if normalized is not None:
        return normalized
    stripped = peer_host.strip()
    return stripped.lower() if stripped else None


def _ip_in_trusted_networks(
    ip_literal: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip_literal(ip_literal)
    if normalized is None:
        return False
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(addr in network for network in trusted_networks)


def _split_forwarding_chain(raw: str) -> list[str] | None:
    if not raw.strip():
        return []
    parts = [segment.strip() for segment in raw.split(",")]
    if any(not segment for segment in parts):
        return None
    if len(parts) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return parts


def _parse_forwarded_header(raw: str) -> str | None:
    for match in _FORWARDED_FOR_RE.finditer(raw):
        for group in match.groups():
            if group:
                normalized = normalize_ip_literal(group)
                if normalized is not None:
                    return normalized
    return None


def _resolve_from_xff_trusted_chain(
    *,
    forwarding_chain: list[str],
    immediate_peer: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    chain = [*forwarding_chain, immediate_peer]
    for hop in reversed(chain):
        normalized = normalize_ip_literal(hop)
        if normalized is None:
            return None
        if not _ip_in_trusted_networks(normalized, trusted_networks):
            return normalized
    return None


def _record_untrusted_forwarding_attempt() -> None:
    global _untrusted_telemetry_last_logged
    now = time.monotonic()
    if now - _untrusted_telemetry_last_logged < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
        return
    if random.random() > 0.1:
        return
    _untrusted_telemetry_last_logged = now
    _logger.info(
        "Admin login source resolution ignored untrusted forwarding headers",
        extra={"resolution_path": SourceResolutionPath.UNTRUSTED_PEER.value},
    )


def _emit_resolution_telemetry(path: SourceResolutionPath) -> None:
    if path in {
        SourceResolutionPath.MALFORMED_FALLBACK,
        SourceResolutionPath.ALL_TRUSTED_FALLBACK,
        SourceResolutionPath.MISSING_PEER,
    }:
        _logger.info(
            "Admin login client source resolved conservatively",
            extra={"resolution_path": path.value},
        )
        return
    _logger.debug(
        "Admin login client source resolved",
        extra={"resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve limiter source identity with verified trusted-proxy boundaries.

    Production chain (documented): browser → Cloudflare → Render load balancer →
    Uvicorn. Forwarded identity is accepted only when ``request.client.host`` is a
    member of ``ADMIN_TRUSTED_PROXY_IPS``. Vendor headers such as
    ``CF-Connecting-IP`` are never honored on direct peers.

    Header precedence (after trusted-peer check):

    1. ``CF-Connecting-IP`` when syntactically valid
    2. ``X-Forwarded-For`` parsed right-to-left across trusted hops
    3. ``Forwarded`` (RFC 7239) first ``for=""`` target
    4. Immediate peer, else ``unknown``
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    peer_host = request.client.host if request.client is not None else None
    if peer_host is None:
        result = ClientSourceResolution("unknown", SourceResolutionPath.MISSING_PEER)
        _emit_resolution_telemetry(result.path)
        return result

    peer_normalized = _peer_identity(peer_host)
    if peer_normalized is None:
        result = ClientSourceResolution("unknown", SourceResolutionPath.MALFORMED_FALLBACK)
        _emit_resolution_telemetry(result.path)
        return result

    peer_trusted = bool(trusted_networks) and _ip_in_trusted_networks(
        peer_normalized, trusted_networks
    )
    if not peer_trusted:
        if request.headers.get("x-forwarded-for") or request.headers.get("forwarded") or (
            request.headers.get("cf-connecting-ip")
        ):
            _record_untrusted_forwarding_attempt()
        result = ClientSourceResolution(peer_normalized, SourceResolutionPath.DIRECT_PEER)
        _emit_resolution_telemetry(result.path)
        return result

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        cf_client = normalize_ip_literal(cf_header)
        if cf_client is not None:
            result = ClientSourceResolution(cf_client, SourceResolutionPath.CF_CONNECTING_IP)
            _emit_resolution_telemetry(result.path)
            return result

    xff_raw = request.headers.get("x-forwarded-for", "")
    if xff_raw:
        forwarding_chain = _split_forwarding_chain(xff_raw)
        if forwarding_chain is None:
            result = ClientSourceResolution(
                peer_normalized, SourceResolutionPath.MALFORMED_FALLBACK
            )
            _emit_resolution_telemetry(result.path)
            return result
        if forwarding_chain:
            xff_client = _resolve_from_xff_trusted_chain(
                forwarding_chain=forwarding_chain,
                immediate_peer=peer_normalized,
                trusted_networks=trusted_networks,
            )
            if xff_client is not None:
                result = ClientSourceResolution(xff_client, SourceResolutionPath.XFF_TRUSTED_CHAIN)
                _emit_resolution_telemetry(result.path)
                return result
            result = ClientSourceResolution(
                peer_normalized, SourceResolutionPath.ALL_TRUSTED_FALLBACK
            )
            _emit_resolution_telemetry(result.path)
            return result

    forwarded_raw = request.headers.get("forwarded", "")
    if forwarded_raw:
        forwarded_client = _parse_forwarded_header(forwarded_raw)
        if forwarded_client is not None:
            result = ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED_HEADER)
            _emit_resolution_telemetry(result.path)
            return result
        result = ClientSourceResolution(peer_normalized, SourceResolutionPath.MALFORMED_FALLBACK)
        _emit_resolution_telemetry(result.path)
        return result

    result = ClientSourceResolution(peer_normalized, SourceResolutionPath.DIRECT_PEER)
    _emit_resolution_telemetry(result.path)
    return result


def reset_untrusted_forwarding_telemetry_for_tests() -> None:
    """Reset sampled telemetry gate (tests only)."""
    global _untrusted_telemetry_last_logged
    _untrusted_telemetry_last_logged = 0.0
