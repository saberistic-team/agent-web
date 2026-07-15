"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative bound on comma-separated forwarding chains (RFC 7239 guidance).
_MAX_FORWARD_CHAIN_LENGTH = 32

# Sample one operational log per N rejected forwarding-header attempts.
_REJECTED_FORWARDING_LOG_SAMPLE_EVERY = 100

_rejected_forwarding_log_lock = threading.Lock()
_rejected_forwarding_log_total = 0

# Render internal peers that terminate TLS before the app process.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "127.0.0.1",
    "10.0.0.0/8",
    "172.16.0.0/12",
)

# Snapshot of Cloudflare published IPv4 ranges (https://www.cloudflare.com/ips-v4).
# Extend via ``ADMIN_TRUSTED_PROXY_CIDRS`` when Cloudflare publishes updates.
DEFAULT_CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
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

# Production request chain documented in docs/ADMIN_AUTH.md:
# browser -> Cloudflare edge -> Render load balancer -> Uvicorn (no proxy rewrite).
PRODUCTION_REQUEST_CHAIN = (
    "public_client",
    "cloudflare_edge",
    "render_load_balancer",
    "uvicorn_process",
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER_IGNORE_HEADERS = "untrusted_peer_ignore_headers"
    XFF_RIGHT_TO_LEFT = "xff_right_to_left"
    FORWARDED_RFC7239 = "forwarded_rfc7239"
    CF_CONNECTING_IP_VERIFIED = "cf_connecting_ip_verified"
    MALFORMED_CONSERVATIVE = "malformed_conservative"
    UNKNOWN_PEER = "unknown_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without raw forwarding material."""

    source: str
    path: SourceResolutionPath


_IPV4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d+$")
_BRACKETED_IPV6_PORT_RE = re.compile(r"^\[([0-9a-fA-F:]+)\]:\d+$")


def parse_client_address(value: str) -> str | None:
    """Normalize one IP literal; return ``None`` for malformed input."""
    candidate = value.strip()
    if not candidate:
        return None

    bracket_match = _BRACKETED_IPV6_PORT_RE.match(candidate)
    if bracket_match:
        candidate = bracket_match.group(1)

    port_match = _IPV4_PORT_RE.match(candidate)
    if port_match:
        candidate = port_match.group(1)

    if candidate.startswith('"') and candidate.endswith('"'):
        candidate = candidate[1:-1].strip()

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def parse_trusted_proxy_networks(cidrs: Iterable[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse configured CIDR literals; skip invalid entries conservatively."""
    networks: list[ipaddress._BaseNetwork] = []
    for raw in cidrs:
        item = raw.strip()
        if not item:
            continue
        try:
            if "/" in item:
                networks.append(ipaddress.ip_network(item, strict=False))
            else:
                networks.append(
                    ipaddress.ip_network(f"{item}/32" if ":" not in item else f"{item}/128", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def is_trusted_proxy_address(address: str, trusted_networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    """Return whether ``address`` is a configured trusted proxy hop."""
    normalized = parse_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _split_forwarded_for(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",")]


def _parse_forwarded_for_chain(header_value: str) -> list[str] | None:
    parts = _split_forwarded_for(header_value)
    if not parts:
        return None
    if len(parts) > _MAX_FORWARD_CHAIN_LENGTH:
        return None

    parsed: list[str] = []
    for part in parts:
        if not part:
            return None
        address = parse_client_address(part)
        if address is None:
            return None
        parsed.append(address)
    return parsed


def _parse_forwarded_header(header_value: str) -> list[str] | None:
    """Parse RFC 7239 ``Forwarded`` header ``for=`` values (first pass)."""
    if not header_value.strip():
        return None

    entries = [entry.strip() for entry in header_value.split(",") if entry.strip()]
    if not entries or len(entries) > _MAX_FORWARD_CHAIN_LENGTH:
        return None

    parsed: list[str] = []
    for entry in entries:
        for token in entry.split(";"):
            token = token.strip()
            if not token.lower().startswith("for="):
                continue
            raw_for = token[4:].strip()
            if raw_for.lower() in {"unknown", "_hidden"}:
                return None
            address = parse_client_address(raw_for)
            if address is None:
                return None
            parsed.append(address)
            break
        else:
            return None
    return parsed or None


def resolve_right_to_left_client(
    chain: list[str],
    *,
    peer: str,
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
) -> str | None:
    """Walk a forwarding chain from the right and return the first untrusted hop."""
    if not chain:
        return None

    full_chain = chain + [peer]
    for address in reversed(full_chain):
        if not is_trusted_proxy_address(address, trusted_networks):
            return address
    return None


def _chain_contains_cloudflare_hop(
    chain: list[str],
    *,
    cloudflare_networks: tuple[ipaddress._BaseNetwork, ...],
) -> bool:
    for address in chain:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if any(parsed in network for network in cloudflare_networks):
            return True
    return False


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _log_rejected_forwarding_headers(path: SourceResolutionPath) -> None:
    global _rejected_forwarding_log_total
    with _rejected_forwarding_log_lock:
        _rejected_forwarding_log_total += 1
        should_log = _rejected_forwarding_log_total % _REJECTED_FORWARDING_LOG_SAMPLE_EVERY == 1
    if should_log:
        _logger.info(
            "Admin login source ignored untrusted forwarding headers",
            extra={
                "source_resolution_path": path.value,
                "rejected_forwarding_sampled": True,
            },
        )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for shared limiter buckets.

    Trust model (production: Cloudflare -> Render LB -> Uvicorn with
    ``--no-proxy-headers``):

    * The immediate TCP peer must match ``ADMIN_TRUSTED_PROXY_CIDRS`` before any
      forwarding header influences the limiter key.
    * ``X-Forwarded-For`` is parsed right-to-left through trusted hops; the
      leftmost raw value is never selected by itself.
    * ``Forwarded`` is used only when ``X-Forwarded-For`` is absent.
    * ``CF-Connecting-IP`` is used only when a verified Cloudflare hop appears in
      the forwarding chain.
    * Direct/untrusted peers always use the socket peer and ignore spoofed headers.
    """
    if request.client is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.UNKNOWN_PEER)

    peer_host = request.client.host.strip()
    peer = parse_client_address(peer_host)
    if peer is None:
        peer = peer_host.lower() if peer_host else "unknown"

    trusted_networks = settings.admin_trusted_proxy_networks
    cloudflare_networks = settings.admin_cloudflare_proxy_networks

    if not settings.admin_trust_proxy_headers or not trusted_networks:
        if _has_forwarding_headers(request):
            _log_rejected_forwarding_headers(SourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    if not is_trusted_proxy_address(peer, trusted_networks):
        if _has_forwarding_headers(request):
            _log_rejected_forwarding_headers(SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS)
        return ClientSourceResolution(peer, SourceResolutionPath.UNTRUSTED_PEER_IGNORE_HEADERS)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        xff_chain = _parse_forwarded_for_chain(xff_header)
        if xff_chain is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.MALFORMED_CONSERVATIVE)
        client = resolve_right_to_left_client(
            xff_chain,
            peer=peer,
            trusted_networks=trusted_networks,
        )
        if client is not None:
            return ClientSourceResolution(client, SourceResolutionPath.XFF_RIGHT_TO_LEFT)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_chain = _parse_forwarded_header(forwarded_header)
        if forwarded_chain is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.MALFORMED_CONSERVATIVE)
        client = resolve_right_to_left_client(
            forwarded_chain,
            peer=peer,
            trusted_networks=trusted_networks,
        )
        if client is not None:
            return ClientSourceResolution(client, SourceResolutionPath.FORWARDED_RFC7239)

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and cloudflare_networks:
        cf_chain = _parse_forwarded_for_chain(xff_header) if xff_header else []
        if cf_chain and _chain_contains_cloudflare_hop(cf_chain, cloudflare_networks=cloudflare_networks):
            cf_client = parse_client_address(cf_header)
            if cf_client is not None:
                return ClientSourceResolution(
                    cf_client,
                    SourceResolutionPath.CF_CONNECTING_IP_VERIFIED,
                )

    return ClientSourceResolution(peer, SourceResolutionPath.MALFORMED_CONSERVATIVE)


def production_trusted_proxy_cidrs() -> tuple[str, ...]:
    """Default production CIDR bundle (Render internal + Cloudflare snapshot)."""
    return DEFAULT_RENDER_TRUSTED_PROXY_CIDRS + DEFAULT_CLOUDFLARE_IPV4_CIDRS
