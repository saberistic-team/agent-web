"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_HEADER_LENGTH = 2048
MAX_FORWARDING_CHAIN_LENGTH = 10

# Sample one invalid/untrusted forwarding telemetry event per interval.
_TELEMETRY_LOCK = threading.Lock()
_TELEMETRY_LAST_EMITTED = 0.0
_TELEMETRY_MIN_INTERVAL_SECONDS = 60.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels; never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    X_FORWARDED_TRUSTED_CHAIN = "xff_trusted_chain"
    CF_CONNECTING_IP_VERIFIED = "cf_connecting_ip_verified"
    FORWARDED_HEADER = "forwarded_header"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and observability metadata."""

    address: str
    path: SourceResolutionPath
    invalid_forwarding: bool = False


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6; strip ports and whitespace."""
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > 128:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate[1:].partition("]")
        if remainder.startswith(":"):
            candidate = host
        else:
            candidate = host

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _address_in_trusted_networks(
    address: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return []
    elements = [part.strip() for part in header_value.split(",") if part.strip()]
    if len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return []
    return elements


def _resolve_from_xff_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    normalized: list[str] = []
    for element in chain:
        address = normalize_ip_address(element)
        if address is None:
            return None
        normalized.append(address)

    if not normalized:
        return None

    while len(normalized) > 1 and _address_in_trusted_networks(
        normalized[-1], trusted_networks
    ):
        normalized.pop()

    return normalized[-1]


def _parse_forwarded_for_header(header_value: str) -> str | None:
    """Extract the first ``for=`` identifier from an RFC 7239 Forwarded header."""
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    for entry in header_value.split(","):
        for directive in entry.split(";"):
            token = directive.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip().strip('"')
            if value.lower() == "unknown":
                continue
            if value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
            return normalize_ip_address(value)
    return None


def _cloudflare_hop_verified(
    xff_chain: list[str],
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    for hop in xff_chain[1:]:
        if _address_in_trusted_networks(hop, cloudflare_networks):
            return True
    return False


def _emit_source_resolution_telemetry(result: ClientSourceResolution) -> None:
    extra = {
        "admin_login_source_path": result.path.value,
        "admin_login_invalid_forwarding": result.invalid_forwarding,
    }
    if result.invalid_forwarding:
        global _TELEMETRY_LAST_EMITTED
        import time

        now = time.monotonic()
        with _TELEMETRY_LOCK:
            if now - _TELEMETRY_LAST_EMITTED < _TELEMETRY_MIN_INTERVAL_SECONDS:
                return
            _TELEMETRY_LAST_EMITTED = now
        _logger.info("Admin login source forwarding rejected", extra=extra)
        return
    _logger.debug("Admin login source resolved", extra=extra)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (saberistic.com): browser → Cloudflare edge → Render load
    balancer → Uvicorn. Forwarded identity is accepted only when the immediate
    TCP peer is in ``ADMIN_TRUSTED_PROXY_CIDRS``. Header precedence when trusted:

    1. ``CF-Connecting-IP`` after a Cloudflare hop is visible in ``X-Forwarded-For``
    2. ``X-Forwarded-For`` parsed right-to-left with trusted-hop stripping
    3. ``Forwarded`` ``for=`` token
    4. Immediate peer address

    Direct peers, missing peers, malformed chains, and spoofed vendor headers fail
    closed to the direct peer (or ``unknown``).
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    hop_networks = settings.admin_forwarding_hop_networks
    cloudflare_networks = settings.admin_cloudflare_edge_networks
    peer_host = request.client.host if request.client is not None else None
    peer_address = normalize_ip_address(peer_host) if peer_host else None
    if peer_address is None and peer_host:
        peer_address = peer_host.strip().lower()

    if peer_address is None:
        result = ClientSourceResolution(
            address="unknown",
            path=SourceResolutionPath.MISSING_PEER,
        )
        _emit_source_resolution_telemetry(result)
        return result

    if not trusted_networks or not _address_in_trusted_networks(
        peer_address, trusted_networks
    ):
        result = ClientSourceResolution(
            address=peer_address,
            path=SourceResolutionPath.DIRECT_PEER,
            invalid_forwarding=_has_spoofed_forwarding_headers(request),
        )
        _emit_source_resolution_telemetry(result)
        return result

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarding_chain(xff_raw) if xff_raw else []
    xff_client = (
        _resolve_from_xff_chain(xff_chain, trusted_networks=hop_networks)
        if xff_chain
        else None
    )

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_client = normalize_ip_address(cf_header) if cf_header else None
    if (
        cf_client is not None
        and xff_chain
        and _cloudflare_hop_verified(xff_chain, cloudflare_networks=cloudflare_networks)
    ):
        result = ClientSourceResolution(
            address=cf_client,
            path=SourceResolutionPath.CF_CONNECTING_IP_VERIFIED,
        )
        _emit_source_resolution_telemetry(result)
        return result

    if xff_client is not None:
        result = ClientSourceResolution(
            address=xff_client,
            path=SourceResolutionPath.X_FORWARDED_TRUSTED_CHAIN,
        )
        _emit_source_resolution_telemetry(result)
        return result

    forwarded_raw = request.headers.get("forwarded", "")
    forwarded_client = (
        _parse_forwarded_for_header(forwarded_raw) if forwarded_raw else None
    )
    if forwarded_client is not None:
        result = ClientSourceResolution(
            address=forwarded_client,
            path=SourceResolutionPath.FORWARDED_HEADER,
        )
        _emit_source_resolution_telemetry(result)
        return result

    if xff_raw or cf_header or forwarded_raw:
        result = ClientSourceResolution(
            address=peer_address,
            path=SourceResolutionPath.INVALID_FORWARDING,
            invalid_forwarding=True,
        )
        _emit_source_resolution_telemetry(result)
        return result

    result = ClientSourceResolution(
        address=peer_address,
        path=SourceResolutionPath.DIRECT_PEER,
    )
    _emit_source_resolution_telemetry(result)
    return result


def _has_spoofed_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def client_ip_from_resolution(resolution: ClientSourceResolution) -> str:
    """Return the limiter source string from a resolution result."""
    return resolution.address


def reset_source_resolution_telemetry_for_tests() -> None:
    """Reset rate-limited telemetry state (tests only)."""
    global _TELEMETRY_LAST_EMITTED
    with _TELEMETRY_LOCK:
        _TELEMETRY_LAST_EMITTED = 0.0
