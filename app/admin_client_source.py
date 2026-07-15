"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings
from app.proxy_trust_config import DEFAULT_TRUSTED_PROXY_CIDRS

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_UNTRUSTED_FORWARDING_LOG_LIMIT_PER_INTERVAL = 5

_logger = logging.getLogger(__name__)
_untrusted_forwarding_log_state: dict[str, tuple[float, int]] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


def _networks_from_cidrs(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses; strip ports and IPv4-mapped IPv6."""
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
        host, sep, port = candidate.partition(":")
        if sep and port.isdigit():
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


def _parse_x_forwarded_for(header_value: str) -> list[str] | None:
    if not header_value.strip():
        return None
    elements = [element.strip() for element in header_value.split(",")]
    if not elements or any(not element for element in elements):
        return None
    if len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return elements


def _parse_forwarded_header(header_value: str) -> list[str] | None:
    """Extract ``for=`` addresses from an RFC 7239 Forwarded header."""
    if not header_value.strip():
        return None

    addresses: list[str] = []
    for entry in header_value.split(","):
        match = re.search(r'for=(?:"\[([^"]+)\]"|"?([^";,\s]+)"?)', entry, flags=re.IGNORECASE)
        if match is None:
            return None
        raw_address = (match.group(1) or match.group(2) or "").strip()
        if not raw_address or raw_address.lower() == "unknown":
            return None
        addresses.append(raw_address)
    if not addresses or len(addresses) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return addresses


def _is_trusted_address(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _resolve_from_forwarding_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping trusted proxy hops."""
    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)

    for hop in reversed(normalized_chain):
        if not _is_trusted_address(hop, trusted_networks):
            return hop
    return None


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    normalized = normalize_ip_address(request.client.host)
    if normalized is not None:
        return normalized
    raw = request.client.host.strip()
    return raw or None


def _trusted_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    cidrs = settings.admin_trusted_proxy_cidrs
    if not cidrs and settings.admin_trust_proxy_headers:
        cidrs = DEFAULT_TRUSTED_PROXY_CIDRS
    return _networks_from_cidrs(cidrs)


def _cloudflare_edge_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _networks_from_cidrs(settings.admin_cloudflare_edge_cidrs)


def _trusted_hop_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks skipped when walking a forwarding chain right-to-left."""
    return _trusted_networks(settings) + _cloudflare_edge_networks(settings)


def _cloudflare_edge_verified(
    chain: list[str] | None,
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not chain or not cloudflare_networks:
        return False
    for hop in chain:
        if _is_trusted_address(hop, cloudflare_networks):
            return True
    return False


def _resolve_cf_connecting_ip(
    request: Request,
    settings: Settings,
    *,
    xff_chain: list[str],
) -> str | None:
    """Accept CF-Connecting-IP only after a configured Cloudflare edge appears in XFF."""
    cf_raw = request.headers.get("cf-connecting-ip", "")
    if not cf_raw:
        return None
    cf_networks = _cloudflare_edge_networks(settings)
    if not cf_networks or not _cloudflare_edge_verified(
        xff_chain,
        cloudflare_networks=cf_networks,
    ):
        return None
    return normalize_ip_address(cf_raw)


def _record_untrusted_forwarding(path: str) -> None:
    """Sample operational telemetry without raw addresses or header values."""
    now = time.monotonic()
    bucket_start, count = _untrusted_forwarding_log_state.get(path, (now, 0))
    if now - bucket_start >= _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
        bucket_start = now
        count = 0
    if count >= _UNTRUSTED_FORWARDING_LOG_LIMIT_PER_INTERVAL:
        _untrusted_forwarding_log_state[path] = (bucket_start, count)
        return
    _untrusted_forwarding_log_state[path] = (bucket_start, count + 1)
    _logger.info(
        "Admin login forwarding headers ignored",
        extra={"resolution_path": path},
    )


def reset_untrusted_forwarding_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    _untrusted_forwarding_log_state.clear()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Trust model:

    * **Untrusted immediate peer** — use the direct peer address; ignore all
      forwarding and vendor headers (``X-Forwarded-For``, ``Forwarded``,
      ``CF-Connecting-IP``).
    * **Trusted immediate peer** — parse ``X-Forwarded-For`` with a
      right-to-left trusted-hop walk. The leftmost value is never trusted on
      its own.
    * **Vendor headers** — ``CF-Connecting-IP`` is accepted only when proxy
      trust is enabled, the immediate peer is trusted, ``X-Forwarded-For`` did
      not yield a client, and a configured Cloudflare edge address appears in
      the forwarding chain.
    * **Forwarded** — RFC 7239 ``Forwarded`` is used only when
      ``X-Forwarded-For`` is absent, using the same right-to-left walk.
    """
    peer = _immediate_peer(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path="missing_peer")

    trusted_networks = _trusted_networks(settings)
    if not settings.admin_trust_proxy_headers or not trusted_networks:
        if _forwarding_headers_present(request):
            _record_untrusted_forwarding("untrusted_peer_forwarding")
        return ClientSourceResolution(source=peer, path="direct_peer")

    if not _is_trusted_address(peer, trusted_networks):
        if _forwarding_headers_present(request):
            _record_untrusted_forwarding("untrusted_peer_forwarding")
        return ClientSourceResolution(source=peer, path="direct_peer")

    xff_raw = request.headers.get("x-forwarded-for", "")
    hop_networks = _trusted_hop_networks(settings)
    if xff_raw:
        xff_chain = _parse_x_forwarded_for(xff_raw)
        if xff_chain is None:
            _record_untrusted_forwarding("malformed_xff")
            return ClientSourceResolution(source=peer, path="malformed_xff")
        resolved = _resolve_from_forwarding_chain(xff_chain, trusted_networks=hop_networks)
        if resolved is not None:
            return ClientSourceResolution(source=resolved, path="trusted_xff")
        cf_fallback = _resolve_cf_connecting_ip(
            request,
            settings,
            xff_chain=xff_chain,
        )
        if cf_fallback is not None:
            return ClientSourceResolution(source=cf_fallback, path="trusted_cf_connecting_ip")
        _record_untrusted_forwarding("malformed_xff")
        return ClientSourceResolution(source=peer, path="malformed_xff")

    cf_raw = request.headers.get("cf-connecting-ip", "")
    if cf_raw:
        _record_untrusted_forwarding("ignored_cf_connecting_ip")
        return ClientSourceResolution(source=peer, path="ignored_cf_connecting_ip")

    forwarded_raw = request.headers.get("forwarded", "")
    forwarded_chain = _parse_forwarded_header(forwarded_raw) if forwarded_raw else None
    if forwarded_chain is not None:
        resolved = _resolve_from_forwarding_chain(
            forwarded_chain,
            trusted_networks=hop_networks,
        )
        if resolved is not None:
            return ClientSourceResolution(source=resolved, path="trusted_forwarded")
        _record_untrusted_forwarding("malformed_forwarded")
        return ClientSourceResolution(source=peer, path="malformed_forwarded")

    if _forwarding_headers_present(request):
        _record_untrusted_forwarding("untrusted_forwarding")
    return ClientSourceResolution(source=peer, path="trusted_peer_fallback")


def _forwarding_headers_present(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )
