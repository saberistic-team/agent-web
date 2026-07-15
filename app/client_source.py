"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from threading import Lock
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative bounds for forwarding metadata (fail closed when exceeded).
MAX_FORWARDING_HEADER_BYTES = 2048
MAX_FORWARDING_CHAIN_LENGTH = 10

# Sample invalid/untrusted forwarding telemetry at most once per interval per path.
_TELEMETRY_SAMPLE_SECONDS = 60.0
_telemetry_lock = Lock()
_telemetry_last_emitted: dict[str, float] = {}

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?:\"([^\"]+)\"|\[([^\]]+)\]|([^;,\s]+))",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry label for how client source was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    TRUSTED_FORWARDED_HEADER = "trusted_forwarded_header"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"
    UNTRUSTED_FORWARDING = "untrusted_forwarding"


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved limiter source identity and the resolution path used."""

    source: str
    path: SourceResolutionPath


@dataclass(frozen=True)
class _TrustedNetworks:
    """Parsed trusted proxy CIDRs/networks from settings."""

    networks: tuple[object, ...]
    edge_networks: tuple[object, ...]


def _parse_network_list(raw: str) -> tuple[object, ...]:
    networks: list[object] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ip_network(candidate, strict=False))
            else:
                networks.append(ip_address(candidate))
        except ValueError:
            continue
    return tuple(networks)


def trusted_networks_from_settings(settings: Settings) -> _TrustedNetworks:
    return _TrustedNetworks(
        networks=_parse_network_list(settings.admin_trusted_proxy_cidrs),
        edge_networks=_parse_network_list(settings.admin_trusted_edge_cidrs),
    )


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 forwarding-chain addresses deterministically."""
    text = raw.strip()
    if not text or len(text) > 128:
        return None

    host = text
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return None
        host = host[1:closing]
        remainder = text[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            return None
    elif host.count(":") == 1 and "." in host:
        host_part, port_part = host.rsplit(":", 1)
        if not port_part.isdigit():
            return None
        host = host_part

    try:
        parsed = ip_address(host.strip())
    except ValueError:
        return None

    if parsed.version == 6:
        mapped = parsed.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return parsed.compressed
    return str(parsed)


def normalize_peer_address(raw: str) -> str | None:
    """Normalize the immediate peer address (IP or opaque local/test client id)."""
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized
    stripped = raw.strip().lower()
    if (
        stripped
        and len(stripped) <= 128
        and " " not in stripped
        and all(ch.isalnum() or ch in ".-_" for ch in stripped)
    ):
        return stripped
    return None


def _address_in_networks(address: str, networks: Iterable[object]) -> bool:
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    for network in networks:
        if isinstance(network, (IPv4Address, IPv6Address)):
            if parsed == network:
                return True
        elif parsed in network:
            return True
    return False


def _is_trusted_proxy(address: str, trusted: _TrustedNetworks) -> bool:
    return _address_in_networks(address, trusted.networks) or _address_in_networks(
        address, trusted.edge_networks
    )


def _has_cloudflare_hop(chain: tuple[str, ...], trusted: _TrustedNetworks) -> bool:
    return any(_address_in_networks(address, trusted.edge_networks) for address in chain)


def _parse_x_forwarded_for(header_value: str) -> tuple[str, ...] | None:
    if len(header_value.encode("utf-8")) > MAX_FORWARDING_HEADER_BYTES:
        return None
    parts = [segment.strip() for segment in header_value.split(",")]
    if not parts or len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    normalized: list[str] = []
    for part in parts:
        if not part:
            return None
        address = normalize_client_address(part)
        if address is None:
            return None
        normalized.append(address)
    return tuple(normalized)


def _parse_forwarded_header(header_value: str) -> tuple[str, ...] | None:
    if len(header_value.encode("utf-8")) > MAX_FORWARDING_HEADER_BYTES:
        return None

    addresses: list[str] = []
    for directive in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN.search(directive.strip())
        if match is None:
            continue
        raw_address = match.group(1) or match.group(2) or match.group(3)
        if raw_address is None:
            continue
        if raw_address.lower() == "unknown":
            continue
        address = normalize_client_address(raw_address)
        if address is None:
            return None
        addresses.append(address)

    if not addresses or len(addresses) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return tuple(addresses)


def _build_forwarding_chain(
    header_chain: tuple[str, ...],
    peer_address: str,
) -> tuple[str, ...]:
    if not header_chain:
        return (peer_address,)
    if header_chain[-1] == peer_address:
        return header_chain
    return header_chain + (peer_address,)


def _resolve_from_trusted_chain(
    chain: tuple[str, ...],
    trusted: _TrustedNetworks,
) -> str | None:
    working = list(chain)
    stripped_edge = False
    while working and _is_trusted_proxy(working[-1], trusted):
        if _address_in_networks(working[-1], trusted.edge_networks):
            stripped_edge = True
        working.pop()
    if not working:
        return None
    if len(working) > 1:
        return None
    if not stripped_edge:
        return None
    return working[-1]


def _should_emit_sampled_telemetry(path: SourceResolutionPath) -> bool:
    now = time.monotonic()
    with _telemetry_lock:
        last = _telemetry_last_emitted.get(path.value, 0.0)
        if now - last < _TELEMETRY_SAMPLE_SECONDS:
            return False
        _telemetry_last_emitted[path.value] = now
    return True


def emit_client_source_telemetry(result: ClientSourceResult) -> None:
    """Emit bounded structured telemetry without raw addresses or headers."""
    invalid_paths = {
        SourceResolutionPath.INVALID_FORWARDING,
        SourceResolutionPath.UNTRUSTED_FORWARDING,
    }
    if result.path in invalid_paths and not _should_emit_sampled_telemetry(result.path):
        return
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": result.path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResult:
    """Resolve the effective client source for admin login rate limiting.

    Trust model (production: Cloudflare → Render load balancer → Uvicorn):

    1. When the immediate peer is **not** in ``ADMIN_TRUSTED_PROXY_CIDRS`` /
       ``ADMIN_TRUSTED_EDGE_CIDRS``, ignore all forwarding headers and use the
       direct peer address. Direct or indirect header spoofing cannot influence
       the limiter key.
    2. When the immediate peer **is** trusted, walk ``X-Forwarded-For`` from the
       right, stripping contiguous trusted proxy hops. A single remaining
       non-trusted address becomes the client. Multiple remaining hops fail
       closed (partial trust).
    3. ``Forwarded`` is consulted only when ``X-Forwarded-For`` does not yield a
       trusted result.
    4. ``CF-Connecting-IP`` is accepted only when the peer is trusted **and** a
       Cloudflare-range hop appears in the validated forwarding chain (proving the
       request transited the public edge).
    """
    trusted = trusted_networks_from_settings(settings)
    if request.client is None:
        return ClientSourceResult(source="unknown", path=SourceResolutionPath.MISSING_PEER)

    peer = normalize_peer_address(request.client.host)
    if peer is None:
        return ClientSourceResult(source="unknown", path=SourceResolutionPath.INVALID_FORWARDING)

    if not trusted.networks and not trusted.edge_networks:
        return ClientSourceResult(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    if not _is_trusted_proxy(peer, trusted):
        return ClientSourceResult(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        parsed_xff = _parse_x_forwarded_for(xff_header)
        if parsed_xff is None:
            return ClientSourceResult(
                source=peer,
                path=SourceResolutionPath.INVALID_FORWARDING,
            )
        chain = _build_forwarding_chain(parsed_xff, peer)
        resolved = _resolve_from_trusted_chain(chain, trusted)
        if resolved is not None:
            return ClientSourceResult(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
            )
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
        )

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        parsed_forwarded = _parse_forwarded_header(forwarded_header)
        if parsed_forwarded is None:
            return ClientSourceResult(
                source=peer,
                path=SourceResolutionPath.INVALID_FORWARDING,
            )
        chain = _build_forwarding_chain(parsed_forwarded, peer)
        resolved = _resolve_from_trusted_chain(chain, trusted)
        if resolved is not None:
            return ClientSourceResult(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_FORWARDED_HEADER,
            )
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
        )

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        cf_address = normalize_client_address(cf_header)
        if cf_address is None:
            return ClientSourceResult(
                source=peer,
                path=SourceResolutionPath.INVALID_FORWARDING,
            )
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
        )

    return ClientSourceResult(source=peer, path=SourceResolutionPath.DIRECT_PEER)


def resolve_admin_login_client_source_with_cf_fallback(
    request: Request,
    settings: Settings,
) -> ClientSourceResult:
    """Resolve client source, applying CF-Connecting-IP only after edge proof."""
    result = resolve_admin_login_client_source(request, settings)
    if result.path != SourceResolutionPath.DIRECT_PEER:
        return result

    trusted = trusted_networks_from_settings(settings)
    if request.client is None:
        return result

    peer = normalize_peer_address(request.client.host)
    if peer is None or not _is_trusted_proxy(peer, trusted):
        return result

    cf_header = request.headers.get("cf-connecting-ip", "")
    if not cf_header:
        return result

    cf_address = normalize_client_address(cf_header)
    if cf_address is None:
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.INVALID_FORWARDING,
        )

    xff_header = request.headers.get("x-forwarded-for", "")
    parsed_xff = _parse_x_forwarded_for(xff_header) if xff_header else None
    if parsed_xff is None:
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
        )

    chain = _build_forwarding_chain(parsed_xff, peer)
    if not _has_cloudflare_hop(chain, trusted):
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
        )

    return ClientSourceResult(
        source=cf_address,
        path=SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
    )


def resolve_client_source(request: Request, settings: Settings) -> ClientSourceResult:
    """Public entry point used by admin auth limiter helpers."""
    primary = resolve_admin_login_client_source(request, settings)
    if primary.path in {
        SourceResolutionPath.TRUSTED_XFF_CHAIN,
        SourceResolutionPath.TRUSTED_FORWARDED_HEADER,
    }:
        emit_client_source_telemetry(primary)
        return primary

    if primary.path in {
        SourceResolutionPath.INVALID_FORWARDING,
        SourceResolutionPath.UNTRUSTED_FORWARDING,
    }:
        emit_client_source_telemetry(primary)
        return primary

    fallback = resolve_admin_login_client_source_with_cf_fallback(request, settings)
    emit_client_source_telemetry(fallback)
    return fallback


def client_source_policy_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification payload for /health."""
    trusted = trusted_networks_from_settings(settings)
    return {
        "mode": "trusted_proxy_cidrs" if trusted.networks or trusted.edge_networks else "direct_peer_only",
        "trusted_proxy_network_count": len(trusted.networks),
        "trusted_edge_network_count": len(trusted.edge_networks),
        "legacy_admin_trust_proxy_headers": settings.admin_trust_proxy_headers,
    }
