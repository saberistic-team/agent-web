"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Render load balancers connect from private/link-local address space.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "127.0.0.1/32",
    "::1/128",
    "fc00::/7",
)

# Published Cloudflare proxy ranges (https://www.cloudflare.com/ips/).
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
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

MAX_FORWARDING_HEADER_LENGTH = 4096
MAX_XFF_CHAIN_LENGTH = 20

_telemetry_lock = threading.Lock()
_telemetry_last_logged: dict[str, float] = {}
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels (no raw addresses or header values)."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER_IGNORE_HEADERS = "untrusted_peer_ignore_headers"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED = "forwarded"
    CF_CONNECTING_IP_CONSISTENT = "cf_connecting_ip_consistent"
    CONFLICTING_HEADERS_CONSERVATIVE = "conflicting_headers_conservative"
    MALFORMED_FORWARDING_CONSERVATIVE = "malformed_forwarding_conservative"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source string and telemetry path."""

    source: str
    path: SourceResolutionPath


def parse_trusted_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def trusted_networks_for_settings(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    cidrs = list(settings.admin_trusted_proxy_cidrs)
    if settings.admin_trust_cloudflare_proxy:
        cidrs.extend(DEFAULT_CLOUDFLARE_PROXY_CIDRS)
    return parse_trusted_networks(cidrs)


def normalize_client_address(raw: str | None) -> str | None:
    """Return a canonical IPv4/IPv6 string, or ``None`` when invalid."""
    if raw is None:
        return None
    value = raw.strip()
    if not value or len(value) > 256:
        return None

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder.startswith(":"):
            if not remainder[1:].isdigit():
                return None
        elif remainder:
            return None
        value = host
    elif value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return None
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


def is_trusted_address(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    ip = ipaddress.ip_address(normalized)
    return any(ip in network for network in trusted_networks)


def parse_x_forwarded_for_chain(header_value: str) -> list[str] | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    hops: list[str] = []
    for part in header_value.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        normalized = normalize_client_address(candidate)
        if normalized is None:
            return None
        hops.append(normalized)
    if len(hops) > MAX_XFF_CHAIN_LENGTH:
        return None
    return hops


def client_from_xff_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    for hop in reversed(chain):
        if not is_trusted_address(hop, trusted_networks):
            return hop
    return chain[0] if chain else None


def parse_forwarded_for(header_value: str) -> str | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    for element in header_value.split(","):
        part = element.strip()
        lower = part.lower()
        if not lower.startswith("for="):
            continue
        value = part[4:].strip()
        if value.startswith('"'):
            closing = value.find('"', 1)
            if closing == -1:
                return None
            value = value[1:closing]
        else:
            value = value.split(";", 1)[0].strip()
        if value.lower() == "unknown":
            continue
        if value.startswith("[") and "]" in value:
            value = value[1 : value.index("]")]
        elif value.count(":") == 1 and "." in value:
            value = value.rsplit(":", 1)[0]
        return normalize_client_address(value)
    return None


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    raw = request.client.host
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized
    stripped = raw.strip()
    if stripped and len(stripped) <= 64:
        return stripped.lower()
    return None


def _log_resolution(path: SourceResolutionPath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"admin_login_source_resolution_path": path.value},
    )


def _log_sampled_warning(event: str) -> None:
    now = time.monotonic()
    with _telemetry_lock:
        last = _telemetry_last_logged.get(event, 0.0)
        if now - last < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            return
        _telemetry_last_logged[event] = now
    _logger.warning(
        "Admin login forwarding header rejected",
        extra={"admin_login_forwarding_event": event},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain: browser → Cloudflare edge → Render load balancer → Uvicorn.

    Forwarding headers are honored only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_CIDRS``. ``X-Forwarded-For`` is parsed from right to
    left, skipping trusted proxy hops (Render private space and, when enabled,
    Cloudflare ranges). ``CF-Connecting-IP`` is accepted only when it matches
    the derived client. Conflicting or malformed forwarding data falls back
    conservatively without exposing details to the caller.
    """
    trusted_networks = trusted_networks_for_settings(settings)
    peer = _peer_host(request)
    if peer is None:
        _log_resolution(SourceResolutionPath.MISSING_PEER)
        return ClientSourceResolution(source="unknown", path=SourceResolutionPath.MISSING_PEER)

    if not trusted_networks or not is_trusted_address(peer, trusted_networks):
        _log_resolution(SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS,
        )

    xff_header = request.headers.get("x-forwarded-for", "")
    forwarded_header = request.headers.get("forwarded", "")
    cf_header = request.headers.get("cf-connecting-ip", "")

    xff_client: str | None = None
    if xff_header:
        chain = parse_x_forwarded_for_chain(xff_header)
        if chain is None:
            _log_sampled_warning("malformed_x_forwarded_for")
            _log_resolution(SourceResolutionPath.MALFORMED_FORWARDING_CONSERVATIVE)
            return ClientSourceResolution(
                source=peer,
                path=SourceResolutionPath.MALFORMED_FORWARDING_CONSERVATIVE,
            )
        xff_client = client_from_xff_chain(chain, trusted_networks)

    forwarded_client: str | None = None
    if forwarded_header:
        forwarded_client = parse_forwarded_for(forwarded_header)
        if forwarded_client is None:
            _log_sampled_warning("malformed_forwarded")
            _log_resolution(SourceResolutionPath.MALFORMED_FORWARDING_CONSERVATIVE)
            return ClientSourceResolution(
                source=peer,
                path=SourceResolutionPath.MALFORMED_FORWARDING_CONSERVATIVE,
            )

    if xff_client and forwarded_client and xff_client != forwarded_client:
        _log_sampled_warning("conflicting_forwarding_headers")
        _log_resolution(SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE,
        )

    derived = xff_client or forwarded_client
    if derived is None:
        _log_resolution(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    cf_client = normalize_client_address(cf_header) if cf_header else None
    if cf_client is not None and cf_client != derived:
        _log_sampled_warning("cf_connecting_ip_mismatch")
        _log_resolution(SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE)
        return ClientSourceResolution(
            source=derived,
            path=SourceResolutionPath.CONFLICTING_HEADERS_CONSERVATIVE,
        )

    if cf_client is not None and cf_client == derived:
        _log_resolution(SourceResolutionPath.CF_CONNECTING_IP_CONSISTENT)
        return ClientSourceResolution(
            source=derived,
            path=SourceResolutionPath.CF_CONNECTING_IP_CONSISTENT,
        )

    path = (
        SourceResolutionPath.X_FORWARDED_FOR
        if xff_client is not None
        else SourceResolutionPath.FORWARDED
    )
    _log_resolution(path)
    return ClientSourceResolution(source=derived, path=path)
