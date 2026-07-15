"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Maximum comma-separated forwarding hops accepted before failing closed.
MAX_FORWARD_CHAIN_LENGTH = 32

# Render and similar PaaS load balancers connect from private networks only.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
    "fc00::/7",
)

# Representative Cloudflare edge ranges (override via ADMIN_CLOUDFLARE_PROXY_CIDRS).
DEFAULT_CLOUDFLARE_PROXY_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)

_UNTRUSTED_FORWARDING_SAMPLE_INTERVAL = 100

_logger = logging.getLogger(__name__)
_untrusted_forwarding_counter = 0
_untrusted_forwarding_lock = threading.Lock()

_FORWARDED_FOR_PARAM = re.compile(
    r"""for=(?:"([^"]+)"|\[([^\]]+)\]|([^";,\s]+))""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    address: str
    path: str
    ignored_untrusted_forwarding: bool = False


def reset_client_source_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _untrusted_forwarding_counter
    with _untrusted_forwarding_lock:
        _untrusted_forwarding_counter = 0


def normalize_ip_address(raw: str | None) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 deterministically."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    if text.startswith("["):
        closing = text.find("]")
        if closing != -1:
            text = text[1:closing]
    elif text.count(":") == 1 and "." in text:
        host, _port = text.rsplit(":", 1)
        if host.replace(".", "").isdigit():
            text = host

    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def parse_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        candidate = cidr.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def ip_in_networks(ip: str, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    normalized = normalize_ip_address(ip)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def transport_peer_host(request: Request) -> str | None:
    """Return the immediate transport peer before proxy-header rewriting."""
    transport_peer = getattr(request.state, "transport_peer", None)
    if isinstance(transport_peer, tuple) and transport_peer:
        return str(transport_peer[0])
    if request.client is not None:
        return request.client.host
    return None


def _trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    cidrs = settings.admin_trusted_proxy_cidrs
    if not cidrs and settings.admin_trust_proxy_headers:
        cidrs = DEFAULT_RENDER_TRUSTED_PROXY_CIDRS
    return parse_networks(cidrs)


def _cloudflare_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not settings.admin_cloudflare_trust_enabled:
        return ()
    cidrs = settings.admin_cloudflare_proxy_cidrs or DEFAULT_CLOUDFLARE_PROXY_CIDRS
    return parse_networks(cidrs)


def _all_trusted_hop_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _trusted_proxy_networks(settings) + _cloudflare_proxy_networks(settings)


def _split_forwarding_chain(raw: str) -> list[str]:
    if not raw.strip():
        return []
    parts = [segment.strip() for segment in raw.split(",")]
    if len(parts) > MAX_FORWARD_CHAIN_LENGTH:
        return []
    return parts


def _parse_x_forwarded_for_chain(raw: str | None) -> list[str]:
    if raw is None:
        return []
    chain: list[str] = []
    for segment in _split_forwarding_chain(raw):
        if not segment:
            continue
        normalized = normalize_ip_address(segment)
        if normalized is None:
            return []
        chain.append(normalized)
    return chain


def _parse_forwarded_header_chain(raw: str | None) -> list[str]:
    if raw is None:
        return []
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if len(entries) > MAX_FORWARD_CHAIN_LENGTH:
        return []
    chain: list[str] = []
    for entry in entries:
        match = _FORWARDED_FOR_PARAM.search(entry)
        if match is None:
            return []
        candidate = match.group(1) or match.group(2) or match.group(3)
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return []
        chain.append(normalized)
    return chain


def _client_from_trusted_chain(
    chain: list[str],
    *,
    trusted_hops: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    if not chain:
        return None
    for hop in reversed(chain):
        if ip_in_networks(hop, trusted_hops):
            continue
        return hop
    return None


def _chain_contains_cloudflare_hop(
    chain: list[str],
    *,
    cloudflare_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    if not cloudflare_networks:
        return False
    return any(ip_in_networks(hop, cloudflare_networks) for hop in chain)


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _record_untrusted_forwarding(path: str) -> None:
    global _untrusted_forwarding_counter
    with _untrusted_forwarding_lock:
        _untrusted_forwarding_counter += 1
        should_log = _untrusted_forwarding_counter % _UNTRUSTED_FORWARDING_SAMPLE_INTERVAL == 1
    if should_log:
        _logger.warning(
            "Admin login forwarding headers ignored",
            extra={"resolution_path": path},
        )


def _log_resolution(path: str) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": path},
    )


def _resolve_peer_address(peer_raw: str | None) -> str | None:
    if peer_raw is None:
        return None
    normalized = normalize_ip_address(peer_raw)
    if normalized is not None:
        return normalized
    stripped = peer_raw.strip()
    return stripped or None


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_raw = transport_peer_host(request)
    peer = _resolve_peer_address(peer_raw)
    if peer is None:
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding("missing_peer_forwarding_ignored")
        _log_resolution("missing_peer")
        return ClientSourceResolution("unknown", "missing_peer")

    trusted_networks = _trusted_proxy_networks(settings)
    if not trusted_networks:
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding("direct_peer_forwarding_ignored")
        _log_resolution("direct_peer")
        return ClientSourceResolution(
            peer,
            "direct_peer",
            ignored_untrusted_forwarding=_has_forwarding_headers(request),
        )

    if not ip_in_networks(peer, trusted_networks):
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding("untrusted_peer_forwarding_ignored")
        _log_resolution("direct_peer")
        return ClientSourceResolution(
            peer,
            "direct_peer",
            ignored_untrusted_forwarding=_has_forwarding_headers(request),
        )

    trusted_hops = _all_trusted_hop_networks(settings)
    cloudflare_networks = _cloudflare_proxy_networks(settings)

    xff_chain = _parse_x_forwarded_for_chain(request.headers.get("x-forwarded-for"))
    forwarded_chain = _parse_forwarded_header_chain(request.headers.get("forwarded"))

    if xff_chain == [] and request.headers.get("x-forwarded-for", "").strip():
        _record_untrusted_forwarding("malformed_x_forwarded_for")
        _log_resolution("malformed_forwarding")
        return ClientSourceResolution(peer, "malformed_forwarding")

    if forwarded_chain == [] and request.headers.get("forwarded", "").strip():
        _record_untrusted_forwarding("malformed_forwarded")
        _log_resolution("malformed_forwarding")
        return ClientSourceResolution(peer, "malformed_forwarding")

    cf_header = request.headers.get("cf-connecting-ip")
    cf_candidate = normalize_ip_address(cf_header) if cf_header else None
    if cf_header and cf_candidate is None:
        _record_untrusted_forwarding("malformed_cf_connecting_ip")
        _log_resolution("malformed_forwarding")
        return ClientSourceResolution(peer, "malformed_forwarding")

    active_chain = xff_chain or forwarded_chain
    if settings.admin_cloudflare_trust_enabled and cf_candidate is not None:
        if _chain_contains_cloudflare_hop(active_chain, cloudflare_networks=cloudflare_networks):
            _log_resolution("cf_connecting_ip")
            return ClientSourceResolution(cf_candidate, "cf_connecting_ip")

    if cf_candidate is not None and not _chain_contains_cloudflare_hop(
        active_chain,
        cloudflare_networks=cloudflare_networks,
    ):
        _record_untrusted_forwarding("cf_connecting_ip_without_cloudflare_hop")

    if xff_chain:
        client = _client_from_trusted_chain(xff_chain, trusted_hops=trusted_hops)
        if client is not None:
            _log_resolution("xff_trusted_chain")
            return ClientSourceResolution(client, "xff_trusted_chain")

    if forwarded_chain:
        client = _client_from_trusted_chain(forwarded_chain, trusted_hops=trusted_hops)
        if client is not None:
            _log_resolution("forwarded_trusted_chain")
            return ClientSourceResolution(client, "forwarded_trusted_chain")

    if active_chain:
        _record_untrusted_forwarding("trusted_peer_only_trusted_hops")
        _log_resolution("trusted_peer_only_trusted_hops")
        return ClientSourceResolution(peer, "trusted_peer_only_trusted_hops")

    _log_resolution("trusted_proxy_peer")
    return ClientSourceResolution(peer, "trusted_proxy_peer")


def proxy_trust_health_summary(settings: Settings) -> dict[str, bool]:
    """Non-sensitive deployment verification fields for /health."""
    trusted_cidrs = settings.admin_trusted_proxy_cidrs
    if not trusted_cidrs and settings.admin_trust_proxy_headers:
        trusted_cidrs = DEFAULT_RENDER_TRUSTED_PROXY_CIDRS
    return {
        "trusted_proxy_cidrs_configured": bool(trusted_cidrs),
        "cloudflare_trust_enabled": settings.admin_cloudflare_trust_enabled,
    }
