"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Production request chain: browser -> Cloudflare edge -> Render proxy -> Uvicorn.
# Only the Render proxy (private RFC1918 / loopback) is an approved immediate peer.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1/128",
    "fc00::/7",
)

# Published Cloudflare edge ranges (subset used to prove CF-Connecting-IP origin).
# Full list: https://www.cloudflare.com/ips-v4 / ips-v6
CLOUDFLARE_EDGE_CIDRS: tuple[str, ...] = (
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

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNKNOWN_SOURCE = "unknown"
_UNTRUSTED_FORWARDING_LOG_INTERVAL = 100

_logger = logging.getLogger(__name__)
_untrusted_forwarding_log_counter = 0

_FORWARDED_FOR_PARAM = re.compile(
    r"""for=(?:"([^"]+)"|\[([^\]]+)\]|([^";,\s]+))""",
    re.IGNORECASE,
)


class SourceResolutionPath(StrEnum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    CLOUDFLARE_CONNECTING_IP = "cloudflare_connecting_ip"
    INVALID_FORWARDING = "invalid_forwarding"
    MISSING_PEER = "missing_peer"
    UNTRUSTED_FORWARDING = "untrusted_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: SourceResolutionPath
    had_forwarding_headers: bool = False


def default_trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_cidr_list(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)


def parse_cidr_list(
    cidrs: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
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


def cloudflare_edge_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_cidr_list(CLOUDFLARE_EDGE_CIDRS)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if settings.admin_trusted_proxy_cidrs:
        return parse_cidr_list(settings.admin_trusted_proxy_cidrs)
    if settings.admin_trust_proxy_headers:
        return default_trusted_proxy_networks()
    return ()


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 deterministically; return None when invalid."""
    value = raw.strip()
    if not value:
        return None

    if value.lower() == "testclient":
        return "testclient"

    if value.startswith("[") and "]" in value:
        host, _, port = value[1:].partition("]")
        if port.startswith(":") and port[1:].isdigit():
            value = host
        else:
            value = host

    if value.count(":") == 1 and value.replace(".", "").replace(":", "").isdigit():
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _ip_in_networks(
    host: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(host)
    if normalized is None or normalized == "testclient":
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _parse_forwarded_for_chain(raw: str) -> list[str] | None:
    if not raw.strip():
        return []
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    hops: list[str] = []
    for part in parts:
        if not part:
            continue
        normalized = normalize_ip_address(part)
        if normalized is None:
            return None
        hops.append(normalized)
    return hops


def _parse_forwarded_header_for(raw: str) -> str | None:
    if not raw.strip():
        return None
    for segment in raw.split(","):
        match = _FORWARDED_FOR_PARAM.search(segment)
        if match is None:
            continue
        candidate = match.group(1) or match.group(2) or match.group(3) or ""
        normalized = normalize_ip_address(candidate)
        if normalized is not None:
            return normalized
    return None


def _cloudflare_edge_present(
    hops: list[str],
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(_ip_in_networks(hop, cloudflare_networks) for hop in hops)


def _walk_trusted_xff_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not hops:
        return None
    remaining = list(hops)
    while remaining:
        rightmost = remaining[-1]
        if _ip_in_networks(rightmost, trusted_networks) or _ip_in_networks(
            rightmost, cloudflare_networks
        ):
            remaining.pop()
            continue
        return rightmost
    return None


def _maybe_log_untrusted_forwarding(path: SourceResolutionPath) -> None:
    global _untrusted_forwarding_log_counter
    _untrusted_forwarding_log_counter += 1
    if _untrusted_forwarding_log_counter % _UNTRUSTED_FORWARDING_LOG_INTERVAL != 1:
        return
    _logger.warning(
        "Admin login source resolution rejected forwarding headers",
        extra={
            "resolution_path": path.value,
            "sampled": True,
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve privacy-preserving client source for admin login limiter buckets.

    Forwarding headers are honored only when the immediate upstream peer is a
    member of ``ADMIN_TRUSTED_PROXY_CIDRS``. Untrusted peers always map to the
    direct connection address so spoofed ``X-Forwarded-For``, ``Forwarded``, and
    ``CF-Connecting-IP`` values cannot rotate limiter buckets.
    """
    trusted_networks = trusted_proxy_networks(settings)
    cloudflare_networks = cloudflare_edge_networks()

    xff_raw = request.headers.get("x-forwarded-for", "")
    forwarded_raw = request.headers.get("forwarded", "")
    cf_connecting_raw = request.headers.get("cf-connecting-ip", "")
    had_forwarding_headers = bool(xff_raw or forwarded_raw or cf_connecting_raw)

    peer_raw = request.client.host if request.client is not None else ""
    peer = normalize_ip_address(peer_raw) if peer_raw else None

    xff_hops = _parse_forwarded_for_chain(xff_raw) if xff_raw else None
    if xff_raw and xff_hops is None:
        _maybe_log_untrusted_forwarding(SourceResolutionPath.INVALID_FORWARDING)
        if peer is None:
            return ClientSourceResolution(
                source=_UNKNOWN_SOURCE,
                path=SourceResolutionPath.MISSING_PEER,
                had_forwarding_headers=had_forwarding_headers,
            )
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.INVALID_FORWARDING,
            had_forwarding_headers=had_forwarding_headers,
        )

    hops: list[str] = list(xff_hops or [])
    if xff_hops is not None:
        if not hops:
            if peer is None:
                return ClientSourceResolution(
                    source=_UNKNOWN_SOURCE,
                    path=SourceResolutionPath.MISSING_PEER,
                    had_forwarding_headers=had_forwarding_headers,
                )
            hops = [peer]
    elif peer is not None:
        hops = [peer]
    else:
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=SourceResolutionPath.MISSING_PEER,
            had_forwarding_headers=had_forwarding_headers,
        )

    proxy_trust_enabled = bool(trusted_networks)
    peer_trusted = peer is not None and _ip_in_networks(peer, trusted_networks)
    peer_is_verified_edge = (
        peer is not None
        and hops
        and len(hops) >= 2
        and peer == hops[-1]
        and (
            _ip_in_networks(peer, trusted_networks)
            or _ip_in_networks(peer, cloudflare_networks)
        )
    )
    can_trust_forwarding = proxy_trust_enabled and (peer_trusted or peer_is_verified_edge)

    if not can_trust_forwarding:
        if had_forwarding_headers and proxy_trust_enabled:
            _maybe_log_untrusted_forwarding(SourceResolutionPath.UNTRUSTED_FORWARDING)
            path = SourceResolutionPath.UNTRUSTED_FORWARDING
        else:
            path = SourceResolutionPath.DIRECT_PEER
        if peer is None:
            return ClientSourceResolution(
                source=_UNKNOWN_SOURCE,
                path=SourceResolutionPath.MISSING_PEER,
                had_forwarding_headers=had_forwarding_headers,
            )
        return ClientSourceResolution(
            source=peer,
            path=path,
            had_forwarding_headers=had_forwarding_headers,
        )

    cf_candidate: str | None = None
    if cf_connecting_raw:
        cf_candidate = normalize_ip_address(cf_connecting_raw)
        if cf_candidate is None:
            _maybe_log_untrusted_forwarding(SourceResolutionPath.INVALID_FORWARDING)
            return ClientSourceResolution(
                source=peer or _UNKNOWN_SOURCE,
                path=SourceResolutionPath.INVALID_FORWARDING,
                had_forwarding_headers=had_forwarding_headers,
            )

    if cf_candidate is not None and _cloudflare_edge_present(hops, cloudflare_networks=cloudflare_networks):
        return ClientSourceResolution(
            source=cf_candidate,
            path=SourceResolutionPath.CLOUDFLARE_CONNECTING_IP,
            had_forwarding_headers=had_forwarding_headers,
        )

    walked = _walk_trusted_xff_chain(
        hops,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    )
    if walked is not None:
        return ClientSourceResolution(
            source=walked,
            path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
            had_forwarding_headers=had_forwarding_headers,
        )

    forwarded_for = _parse_forwarded_header_for(forwarded_raw)
    if forwarded_for is not None:
        return ClientSourceResolution(
            source=forwarded_for,
            path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
            had_forwarding_headers=had_forwarding_headers,
        )

    _maybe_log_untrusted_forwarding(SourceResolutionPath.INVALID_FORWARDING)
    return ClientSourceResolution(
        source=peer or _UNKNOWN_SOURCE,
        path=SourceResolutionPath.INVALID_FORWARDING,
        had_forwarding_headers=had_forwarding_headers,
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Return normalized client source string for admin login limiter buckets."""
    return resolve_admin_login_client_source(request, settings).source


def uvicorn_forwarded_allow_ips_arg(
    settings: Settings | None = None,
    *,
    cidrs: Iterable[str] | None = None,
) -> str:
    """Uvicorn ``--forwarded-allow-ips`` value aligned with app trust boundary."""
    if cidrs is not None:
        values = [value.strip() for value in cidrs if value.strip()]
    elif settings is not None and settings.admin_trusted_proxy_cidrs:
        values = list(settings.admin_trusted_proxy_cidrs)
    else:
        values = list(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)
    return ",".join(values)
