"""Trusted-hop client source resolution for admin login rate limiting."""

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

# Conservative bound for comma-separated forwarding chains.
MAX_FORWARDING_CHAIN_LENGTH = 32

# Sampled telemetry for rejected forwarding headers (no raw addresses).
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0
_invalid_telemetry_lock = Lock()
_last_invalid_telemetry_at = 0.0

# Cloudflare published proxy ranges (https://www.cloudflare.com/ips-v4 / ips-v6).
# Used only for right-to-left chain trimming and CF-Connecting-IP verification.
CLOUDFLARE_PROXY_CIDRS: tuple[str, ...] = (
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
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Production Render peer ranges trusted for forwarded-header parsing.
DEFAULT_PEER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

# Uvicorn --forwarded-allow-ips: immediate peer only (Render internal).
DEFAULT_UVICORN_FORWARDED_ALLOW_IPS: str = ",".join(DEFAULT_PEER_TRUSTED_PROXY_CIDRS)

_FORWARDED_FOR_TOKEN = re.compile(r"^for=(?P<value>(?:\"[^\"]+\")|(?:\[[^\]]+\](?::\d+)?)|(?:[0-9a-fA-F:.]+))", re.I)


class ClientSourceResolutionPath:
    """Bounded telemetry labels — never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    TRUSTED_CHAIN_XFF = "trusted_chain_xff"
    TRUSTED_CHAIN_FORWARDED = "trusted_chain_forwarded"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


def parse_trusted_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse CIDR strings into networks; invalid entries are ignored."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, ports, and IPv4-mapped IPv6 deterministically."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing].strip()
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":"):
            if not remainder[1:].isdigit():
                return None
        elif remainder:
            return None
        candidate = host
    elif candidate.count(":") == 1 and "." in candidate:
        host, separator, port = candidate.partition(":")
        if separator and port.isdigit():
            candidate = host.strip()

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def ip_in_trusted_networks(ip: str, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    """Return whether ``ip`` falls within any trusted network."""
    normalized = normalize_ip_address(ip)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() if host else None


def _parse_x_forwarded_for(header_value: str) -> tuple[str, ...]:
    if not header_value.strip():
        return ()
    parts = [segment.strip() for segment in header_value.split(",")]
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return ()
    chain: list[str] = []
    for part in parts:
        if not part:
            continue
        normalized = normalize_ip_address(part)
        if normalized is None:
            return ()
        chain.append(normalized)
    return tuple(chain)


def _unquote_forwarded_value(value: str) -> str:
    trimmed = value.strip()
    if trimmed.startswith('"') and trimmed.endswith('"') and len(trimmed) >= 2:
        return trimmed[1:-1]
    return trimmed


def _parse_forwarded_header(header_value: str) -> tuple[str, ...]:
    if not header_value.strip():
        return ()
    entries = [segment.strip() for segment in header_value.split(",")]
    if len(entries) > MAX_FORWARDING_CHAIN_LENGTH:
        return ()
    chain: list[str] = []
    for entry in entries:
        if not entry:
            continue
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            return ()
        candidate = _unquote_forwarded_value(match.group("value"))
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return ()
        chain.append(normalized)
    return tuple(chain)


def _walk_trusted_chain(
    chain: tuple[str, ...],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Select the rightmost address that is not a trusted proxy hop."""
    for hop in reversed(chain):
        if not ip_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _chain_has_cloudflare_hop(
    *chains: tuple[str, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    for chain in chains:
        for hop in chain:
            if ip_in_trusted_networks(hop, cloudflare_networks):
                return True
    return False


def _peer_trusted_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    cidrs = settings.admin_trusted_proxy_cidrs or DEFAULT_PEER_TRUSTED_PROXY_CIDRS
    return parse_trusted_networks(cidrs)


def _chain_trusted_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    peer_networks = list(_peer_trusted_networks(settings))
    if settings.admin_trust_cloudflare_proxy:
        peer_networks.extend(parse_trusted_networks(CLOUDFLARE_PROXY_CIDRS))
    return tuple(peer_networks)


def _cloudflare_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not settings.admin_trust_cloudflare_proxy:
        return ()
    return parse_trusted_networks(CLOUDFLARE_PROXY_CIDRS)


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for", "").strip()
        or request.headers.get("forwarded", "").strip()
        or request.headers.get("cf-connecting-ip", "").strip()
    )


def _emit_source_resolution_telemetry(path: str, *, forwarding_rejected: bool = False) -> None:
    extra: dict[str, object] = {"admin_client_source_path": path}
    if forwarding_rejected:
        global _last_invalid_telemetry_at
        now = time.monotonic()
        with _invalid_telemetry_lock:
            if now - _last_invalid_telemetry_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
                return
            _last_invalid_telemetry_at = now
        extra["forwarding_rejected"] = True
    _logger.info("Admin client source resolved", extra=extra)


def _resolve_from_trusted_peer(
    request: Request,
    settings: Settings,
    peer: str,
) -> ClientSourceResolution:
    chain_networks = _chain_trusted_networks(settings)
    cloudflare_networks = _cloudflare_networks(settings)

    xff_chain = _parse_x_forwarded_for(request.headers.get("x-forwarded-for", ""))
    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))

    if xff_chain:
        client = _walk_trusted_chain(xff_chain, chain_networks)
        if client is not None:
            _emit_source_resolution_telemetry(ClientSourceResolutionPath.TRUSTED_CHAIN_XFF)
            return ClientSourceResolution(source=client, path=ClientSourceResolutionPath.TRUSTED_CHAIN_XFF)

    if forwarded_chain:
        client = _walk_trusted_chain(forwarded_chain, chain_networks)
        if client is not None:
            _emit_source_resolution_telemetry(ClientSourceResolutionPath.TRUSTED_CHAIN_FORWARDED)
            return ClientSourceResolution(
                source=client,
                path=ClientSourceResolutionPath.TRUSTED_CHAIN_FORWARDED,
            )

    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    if cf_header and cloudflare_networks:
        combined = xff_chain + forwarded_chain
        if combined and _chain_has_cloudflare_hop(
            xff_chain,
            forwarded_chain,
            cloudflare_networks=cloudflare_networks,
        ):
            normalized_cf = normalize_ip_address(cf_header)
            if normalized_cf is not None:
                _emit_source_resolution_telemetry(ClientSourceResolutionPath.TRUSTED_CF_CONNECTING_IP)
                return ClientSourceResolution(
                    source=normalized_cf,
                    path=ClientSourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
                )

    if _has_forwarding_headers(request):
        _emit_source_resolution_telemetry(
            ClientSourceResolutionPath.TRUSTED_PEER_FALLBACK,
            forwarding_rejected=True,
        )
    else:
        _emit_source_resolution_telemetry(ClientSourceResolutionPath.TRUSTED_PEER_FALLBACK)
    return ClientSourceResolution(source=peer, path=ClientSourceResolutionPath.TRUSTED_PEER_FALLBACK)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate TCP peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. The client address is derived by walking
    ``X-Forwarded-For`` (then ``Forwarded``) right-to-left and skipping trusted
    proxy hops. ``CF-Connecting-IP`` is accepted only when a Cloudflare hop is
    present in the verified forwarding chain.
    """
    peer_raw = _peer_host(request)
    if peer_raw is None:
        _emit_source_resolution_telemetry(ClientSourceResolutionPath.MISSING_PEER)
        return ClientSourceResolution(source="unknown", path=ClientSourceResolutionPath.MISSING_PEER)

    peer = normalize_ip_address(peer_raw) or peer_raw

    if not settings.admin_trust_proxy_headers:
        _emit_source_resolution_telemetry(ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=peer, path=ClientSourceResolutionPath.DIRECT_PEER)

    peer_networks = _peer_trusted_networks(settings)
    if not ip_in_trusted_networks(peer, peer_networks):
        if _has_forwarding_headers(request):
            _emit_source_resolution_telemetry(
                ClientSourceResolutionPath.UNTRUSTED_PEER,
                forwarding_rejected=True,
            )
        else:
            _emit_source_resolution_telemetry(ClientSourceResolutionPath.UNTRUSTED_PEER)
        return ClientSourceResolution(source=peer, path=ClientSourceResolutionPath.UNTRUSTED_PEER)

    return _resolve_from_trusted_peer(request, settings, peer)


def reset_source_resolution_telemetry() -> None:
    """Reset sampled invalid-forwarding telemetry (tests only)."""
    global _last_invalid_telemetry_at
    with _invalid_telemetry_lock:
        _last_invalid_telemetry_at = 0.0
