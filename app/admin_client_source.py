"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32

_FORWARDED_FOR_TOKEN = re.compile(
    r"""for=(?P<value>(?:"(?:[^"\\]|\\.)*")|\[[^\]]+\]|[^;,\s"]+)""",
    re.IGNORECASE,
)

_untrusted_forwarding_attempts = 0
_invalid_forwarding_attempts = 0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Limiter source identity and a privacy-safe resolution path label."""

    source: str
    path: str


def normalize_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.lower() == "unknown":
        return "unknown"
    if candidate.startswith('"') and candidate.endswith('"'):
        candidate = candidate[1:-1].strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":") and remainder[1:].isdigit():
            candidate = host
        else:
            candidate = host
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
    return parsed.compressed.lower()


@lru_cache(maxsize=16)
def _networks_from_cidrs(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def address_in_networks(address: str, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _peer_matches_trusted_networks(
    peer: str | None,
    peer_normalized: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if peer and normalize_address(peer) is None:
        return False
    return address_in_networks(peer_normalized, trusted_networks)


def parse_forwarded_header(value: str) -> list[str]:
    """Extract ``for=`` addresses from an RFC 7239 ``Forwarded`` header."""
    chain: list[str] = []
    for match in _FORWARDED_FOR_TOKEN.finditer(value):
        normalized = normalize_address(match.group("value"))
        if normalized is not None:
            chain.append(normalized)
    return chain


def parse_x_forwarded_for(value: str) -> list[str]:
    """Parse a comma-separated ``X-Forwarded-For`` chain."""
    chain: list[str] = []
    for part in value.split(","):
        normalized = normalize_address(part)
        if normalized is not None:
            chain.append(normalized)
    return chain


def _peer_host(request: Request) -> str | None:
    client = request.scope.get("client")
    if client is None:
        return None
    host = client[0]
    if not isinstance(host, str):
        return None
    return host


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in ("forwarded", "x-forwarded-for", "cf-connecting-ip")
    )


def _cloudflare_path_verified(
    request: Request,
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    forwarded_chain = parse_forwarded_header(request.headers.get("forwarded", ""))
    xff_chain = parse_x_forwarded_for(request.headers.get("x-forwarded-for", ""))
    for address in (*forwarded_chain, *xff_chain):
        if address_in_networks(address, cloudflare_networks):
            return True
    return False


def _resolve_from_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not chain:
        return None
    skip_networks = (*trusted_networks, *cloudflare_networks)
    for address in reversed(chain):
        if not address_in_networks(address, skip_networks):
            return address
    return chain[0]


def _extract_forward_chain(
    request: Request,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[list[str], str]:
    forwarded_value = request.headers.get("forwarded", "").strip()
    if forwarded_value:
        chain = parse_forwarded_header(forwarded_value)
        if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
            return [], "malformed_headers"
        if chain and not _chain_is_cloudflare_only(chain, cloudflare_networks):
            return chain, "forwarded_rfc7239"

    xff_value = request.headers.get("x-forwarded-for", "").strip()
    if xff_value:
        chain = parse_x_forwarded_for(xff_value)
        if len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
            return [], "malformed_headers"
        if chain and not _chain_is_cloudflare_only(chain, cloudflare_networks):
            return chain, "xff_right_to_left"

    cf_value = request.headers.get("cf-connecting-ip", "").strip()
    if cf_value and _cloudflare_path_verified(
        request,
        cloudflare_networks=cloudflare_networks,
    ):
        normalized = normalize_address(cf_value)
        if normalized is not None:
            return [normalized], "cf_connecting_ip_verified"

    if xff_value:
        chain = parse_x_forwarded_for(xff_value)
        if chain:
            return chain, "xff_right_to_left"

    if forwarded_value:
        chain = parse_forwarded_header(forwarded_value)
        if chain:
            return chain, "forwarded_rfc7239"

    return [], "trusted_peer_no_headers"


def _chain_is_cloudflare_only(
    chain: list[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not chain or not cloudflare_networks:
        return False
    return all(address_in_networks(address, cloudflare_networks) for address in chain)


def _record_untrusted_forwarding_telemetry() -> None:
    global _untrusted_forwarding_attempts
    _untrusted_forwarding_attempts += 1
    if _untrusted_forwarding_attempts == 1 or _untrusted_forwarding_attempts % 100 == 0:
        _logger.warning(
            "Admin login ignored forwarding headers from untrusted peer",
            extra={
                "resolution_path": "untrusted_forwarding",
                "sample_count": _untrusted_forwarding_attempts,
            },
        )


def _record_invalid_forwarding_telemetry() -> None:
    global _invalid_forwarding_attempts
    _invalid_forwarding_attempts += 1
    if _invalid_forwarding_attempts == 1 or _invalid_forwarding_attempts % 100 == 0:
        _logger.warning(
            "Admin login rejected malformed forwarding headers",
            extra={
                "resolution_path": "malformed_headers",
                "sample_count": _invalid_forwarding_attempts,
            },
        )


def record_client_source_resolution(path: str) -> None:
    """Emit bounded structured telemetry without raw addresses or header values."""
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": path},
    )


def reset_client_source_telemetry_counters() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _untrusted_forwarding_attempts, _invalid_forwarding_attempts
    _untrusted_forwarding_attempts = 0
    _invalid_forwarding_attempts = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source behind trusted proxies."""
    peer = _peer_host(request)
    peer_normalized = normalize_address(peer) if peer else None
    if peer_normalized is None and peer:
        peer_normalized = peer.strip().lower() or "unknown"
    trusted_networks = _networks_from_cidrs(settings.admin_trusted_proxy_cidrs)
    cloudflare_networks = _networks_from_cidrs(settings.admin_trusted_cloudflare_cidrs)

    if peer_normalized is None:
        return ClientSourceResolution("unknown", "missing_peer")

    if not trusted_networks or not _peer_matches_trusted_networks(
        peer,
        peer_normalized,
        trusted_networks,
    ):
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding_telemetry()
        return ClientSourceResolution(peer_normalized, "direct_peer")

    chain, chain_path = _extract_forward_chain(
        request,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    )
    if chain_path == "malformed_headers":
        _record_invalid_forwarding_telemetry()
        return ClientSourceResolution("unknown", "malformed_headers")
    if not chain:
        return ClientSourceResolution(peer_normalized, chain_path)

    resolved = _resolve_from_chain(
        chain,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    )
    if resolved is None:
        _record_invalid_forwarding_telemetry()
        return ClientSourceResolution("unknown", "malformed_headers")
    return ClientSourceResolution(resolved, chain_path)
