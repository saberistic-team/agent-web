"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative bounds for forwarded header parsing.
MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_FORWARDED_HEADER_LENGTH = 2048

# Sampled telemetry for invalid/untrusted forwarding attempts (no raw IPs).
_INVALID_FORWARDING_SAMPLE_INTERVAL = 100
_invalid_forwarding_attempts = 0

# Resolution paths surfaced in structured logs (no addresses).
RESOLUTION_DIRECT_PEER = "direct_peer"
RESOLUTION_TRUSTED_CHAIN = "trusted_chain"
RESOLUTION_UNTRUSTED_FORWARDING = "untrusted_forwarding"
RESOLUTION_MALFORMED_FORWARDING = "malformed_forwarding"
RESOLUTION_MISSING_PEER = "missing_peer"
RESOLUTION_CF_CONNECTING_IP = "cf_connecting_ip"

# Production request chain (documented in docs/ADMIN_AUTH.md):
#   browser -> Cloudflare edge -> Render load balancer -> Uvicorn
#
# Header precedence when the immediate peer is a verified trusted proxy:
#   1. X-Forwarded-For (right-to-left trusted-hop walk)
#   2. Forwarded ``for=`` (RFC 7239) when X-Forwarded-For is absent
#   3. CF-Connecting-IP only after a Cloudflare hop is verified in the chain


@dataclass(frozen=True)
class SourceResolution:
    """Limiter source identity and the resolution path used."""

    source: str
    path: str


@dataclass(frozen=True)
class _ParsedNetwork:
    network: ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_network(cidr: str) -> _ParsedNetwork | None:
    candidate = cidr.strip()
    if not candidate:
        return None
    try:
        if "/" in candidate:
            network = ipaddress.ip_network(candidate, strict=False)
        else:
            address = ipaddress.ip_address(candidate)
            prefix = 32 if address.version == 4 else 128
            network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
        return _ParsedNetwork(network=network)
    except ValueError:
        return None


@lru_cache(maxsize=8)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple[_ParsedNetwork, ...]:
    parsed: list[_ParsedNetwork] = []
    for cidr in cidrs:
        network = _parse_network(cidr)
        if network is not None:
            parsed.append(network)
    return tuple(parsed)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses for deterministic limiter keys."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        # IPv4 with port (203.0.113.1:443)
        candidate = candidate.rsplit(":", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return address.compressed
    return str(address)


def is_trusted_proxy_address(address: str, trusted_networks: Iterable[_ParsedNetwork]) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    for entry in trusted_networks:
        if parsed in entry.network:
            return True
    return False


def _split_forwarded_for(header_value: str) -> list[str] | None:
    if len(header_value) > MAX_FORWARDED_HEADER_LENGTH:
        return None
    elements = [part.strip() for part in header_value.split(",")]
    if not elements or any(not element for element in elements):
        return None
    if len(elements) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return elements


def _parse_forwarded_header(header_value: str) -> list[str] | None:
    if len(header_value) > MAX_FORWARDED_HEADER_LENGTH:
        return None
    addresses: list[str] = []
    for entry in header_value.split(","):
        fragment = entry.strip()
        if not fragment:
            return None
        for token in fragment.split(";"):
            token = token.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip().strip('"')
            if value.lower() in {"unknown", "-"}:
                continue
            normalized = normalize_ip_address(value)
            if normalized is None:
                return None
            addresses.append(normalized)
    if not addresses or len(addresses) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return addresses


def _cloudflare_hop_verified(
    chain: list[str],
    *,
    peer: str,
    trusted_networks: tuple[_ParsedNetwork, ...],
    cloudflare_networks: tuple[_ParsedNetwork, ...],
) -> bool:
    hops = [*chain, peer]
    for hop in reversed(hops):
        if is_trusted_proxy_address(hop, cloudflare_networks):
            return True
        if not is_trusted_proxy_address(hop, trusted_networks):
            break
    return False


def _resolve_from_trusted_chain(
    hops: list[str],
    *,
    peer: str,
    trusted_networks: tuple[_ParsedNetwork, ...],
) -> str | None:
    ordered = [*hops, peer]
    for hop in reversed(ordered):
        if is_trusted_proxy_address(hop, trusted_networks):
            continue
        normalized = normalize_ip_address(hop)
        if normalized is not None:
            return normalized
        return None
    return None


def _record_invalid_forwarding(path: str) -> None:
    global _invalid_forwarding_attempts
    _invalid_forwarding_attempts += 1
    if _invalid_forwarding_attempts % _INVALID_FORWARDING_SAMPLE_INTERVAL == 1:
        _logger.warning(
            "Admin login source resolution rejected forwarding headers",
            extra={
                "resolution_path": path,
                "sampled_invalid_forwarding_count": _invalid_forwarding_attempts,
            },
        )


def reset_source_resolution_telemetry() -> None:
    """Reset sampled invalid-forwarding counters (tests only)."""
    global _invalid_forwarding_attempts
    _invalid_forwarding_attempts = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> SourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    if request.client is None:
        return SourceResolution(source="unknown", path=RESOLUTION_MISSING_PEER)

    peer_raw = request.client.host
    peer = normalize_ip_address(peer_raw)
    if peer is None:
        if not settings.admin_trust_proxy_headers or not settings.admin_trusted_proxy_cidrs:
            return SourceResolution(
                source=peer_raw.strip() or "unknown",
                path=RESOLUTION_DIRECT_PEER,
            )
        return SourceResolution(source="unknown", path=RESOLUTION_MISSING_PEER)

    trusted_cidrs = settings.admin_trusted_proxy_cidrs
    if not settings.admin_trust_proxy_headers or not trusted_cidrs:
        return SourceResolution(source=peer, path=RESOLUTION_DIRECT_PEER)

    trusted_networks = _trusted_networks(trusted_cidrs)
    if not trusted_networks:
        return SourceResolution(source=peer, path=RESOLUTION_DIRECT_PEER)

    if not is_trusted_proxy_address(peer, trusted_networks):
        if _request_has_forwarding_headers(request):
            _record_invalid_forwarding(RESOLUTION_UNTRUSTED_FORWARDING)
        return SourceResolution(source=peer, path=RESOLUTION_UNTRUSTED_FORWARDING)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        chain = _split_forwarded_for(xff_header)
        if chain is None:
            _record_invalid_forwarding(RESOLUTION_MALFORMED_FORWARDING)
            return SourceResolution(source=peer, path=RESOLUTION_MALFORMED_FORWARDING)
        resolved = _resolve_from_trusted_chain(
            chain,
            peer=peer,
            trusted_networks=trusted_networks,
        )
        if resolved is not None:
            return SourceResolution(source=resolved, path=RESOLUTION_TRUSTED_CHAIN)
        _record_invalid_forwarding(RESOLUTION_MALFORMED_FORWARDING)
        return SourceResolution(source=peer, path=RESOLUTION_MALFORMED_FORWARDING)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        chain = _parse_forwarded_header(forwarded_header)
        if chain is None:
            _record_invalid_forwarding(RESOLUTION_MALFORMED_FORWARDING)
            return SourceResolution(source=peer, path=RESOLUTION_MALFORMED_FORWARDING)
        resolved = _resolve_from_trusted_chain(
            chain,
            peer=peer,
            trusted_networks=trusted_networks,
        )
        if resolved is not None:
            return SourceResolution(source=resolved, path=RESOLUTION_TRUSTED_CHAIN)
        _record_invalid_forwarding(RESOLUTION_MALFORMED_FORWARDING)
        return SourceResolution(source=peer, path=RESOLUTION_MALFORMED_FORWARDING)

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        cf_networks = _trusted_networks(settings.admin_cloudflare_proxy_cidrs)
        if cf_networks and _cloudflare_hop_verified(
            [],
            peer=peer,
            trusted_networks=trusted_networks,
            cloudflare_networks=cf_networks,
        ):
            normalized = normalize_ip_address(cf_header)
            if normalized is not None:
                return SourceResolution(source=normalized, path=RESOLUTION_CF_CONNECTING_IP)
        _record_invalid_forwarding(RESOLUTION_UNTRUSTED_FORWARDING)

    return SourceResolution(source=peer, path=RESOLUTION_DIRECT_PEER)


def _request_has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def log_source_resolution(resolution: SourceResolution) -> None:
    """Emit bounded structured telemetry without raw addresses."""
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": resolution.path},
    )
