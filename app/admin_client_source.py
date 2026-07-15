"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Published Cloudflare anycast ranges (https://www.cloudflare.com/ips/).
# Used only to skip trusted hops in X-Forwarded-For and to validate CF-Connecting-IP.
_CLOUDFLARE_EDGE_CIDRS: tuple[str, ...] = (
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "108.162.192.0/18",
    "131.0.72.0/22",
    "141.101.64.0/18",
    "162.158.0.0/15",
    "172.64.0.0/13",
    "173.245.48.0/20",
    "188.114.96.0/20",
    "190.93.240.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Render / private-network peers that terminate TLS before Uvicorn.
_DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "127.0.0.1/32",
    "::1/128",
)

_MAX_FORWARDING_CHAIN_LENGTH = 32
_MAX_HEADER_LENGTH = 2048

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\")|\[(?:[^\]]+)\]|(?:[\dA-Fa-f:.]+))(?:;\s*|$)",
    re.IGNORECASE,
)

_INVALID_TELEMETRY_WINDOW_SECONDS = 300
_INVALID_TELEMETRY_SAMPLE_LIMIT = 20
_invalid_telemetry_state = {"window_start": 0.0, "count": 0, "sampled": 0}


class SourceResolutionPath(StrEnum):
    """Bounded telemetry labels — no raw addresses or header values."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_X_FORWARDED_FOR = "trusted_x_forwarded_for"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    UNTRUSTED_HEADERS_IGNORED = "untrusted_headers_ignored"
    INVALID_FORWARDING_DATA = "invalid_forwarding_data"
    FALLBACK_UNKNOWN = "fallback_unknown"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: SourceResolutionPath


def parse_cidr_list(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDR strings; skip empty or invalid entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return configured trusted proxy CIDR blocks plus optional Cloudflare edge ranges."""
    configured = parse_cidr_list(settings.admin_trusted_proxy_cidrs)
    if configured:
        base = configured
    elif settings.admin_trust_proxy_headers:
        base = parse_cidr_list(",".join(_DEFAULT_RENDER_TRUSTED_PROXY_CIDRS))
    else:
        base = ()

    if not settings.admin_trust_cloudflare_edge:
        return base

    cloudflare = parse_cidr_list(",".join(_CLOUDFLARE_EDGE_CIDRS))
    merged: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = list(base)
    seen = {str(net) for net in base}
    for network in cloudflare:
        key = str(network)
        if key not in seen:
            merged.append(network)
            seen.add(key)
    return tuple(merged)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 strings deterministically; reject malformed input."""
    if not raw:
        return None
    value = raw.strip().strip('"').strip()
    if not value or len(value) > 128:
        return None

    if value.startswith("[") and "]" in value:
        host, _, port = value[1:].partition("]")
        if port.startswith(":"):
            port = port[1:]
            if port and not port.isdigit():
                return None
        value = host.strip()
    elif value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host.strip()
        else:
            return None

    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    for network in networks:
        if address.version != network.version:
            continue
        if address in network:
            return True
    return False


def is_trusted_proxy_address(
    address_text: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip_address(address_text)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return _address_in_networks(address, networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    if len(header_value) > _MAX_HEADER_LENGTH:
        return []
    parts = [segment.strip() for segment in header_value.split(",")]
    if len(parts) > _MAX_FORWARDING_CHAIN_LENGTH:
        return []
    return parts


def _walk_x_forwarded_for(
    header_value: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    chain = _split_forwarding_chain(header_value)
    if not chain:
        return None

    normalized_chain: list[str] = []
    for hop in chain:
        if not hop:
            return None
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)

    for hop in reversed(normalized_chain):
        if not is_trusted_proxy_address(hop, networks):
            return hop
    return None


def _parse_forwarded_header(
    header_value: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    if len(header_value) > _MAX_HEADER_LENGTH:
        return None

    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            continue
        candidate = match.group("value").strip('"')
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return None
        if not is_trusted_proxy_address(normalized, networks):
            return normalized
    return None


def _cloudflare_hop_present(
    header_value: str,
    cloudflare_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    chain = _split_forwarding_chain(header_value)
    for hop in chain:
        if hop and is_trusted_proxy_address(hop, cloudflare_networks):
            return True
    return False


def _record_invalid_forwarding_telemetry(path: SourceResolutionPath) -> None:
    now = time.monotonic()
    if now - _invalid_telemetry_state["window_start"] >= _INVALID_TELEMETRY_WINDOW_SECONDS:
        _invalid_telemetry_state["window_start"] = now
        _invalid_telemetry_state["count"] = 0
        _invalid_telemetry_state["sampled"] = 0

    _invalid_telemetry_state["count"] += 1
    if _invalid_telemetry_state["sampled"] >= _INVALID_TELEMETRY_SAMPLE_LIMIT:
        return

    _invalid_telemetry_state["sampled"] += 1
    _logger.info(
        "Admin login client source ignored invalid or untrusted forwarding data",
        extra={
            "resolution_path": path.value,
            "invalid_forwarding_sample_index": _invalid_telemetry_state["sampled"],
            "invalid_forwarding_window_count": _invalid_telemetry_state["count"],
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    the configured trusted-proxy boundary. Client identity is derived by walking
    forwarding chains from the trusted edge inward (right-to-left for
    ``X-Forwarded-For``), never by trusting a leftmost attacker-supplied value
    from an unverified peer.
    """
    peer_host = request.client.host if request.client is not None else None
    peer = normalize_ip_address(peer_host or "")
    if peer is None and peer_host:
        # ASGI test transports may use non-IP peer names (e.g. ``testclient``).
        cleaned = peer_host.strip().lower()
        peer = cleaned[:128] if cleaned else None
    if peer is None:
        return ClientSourceResolution(source="unknown", path=SourceResolutionPath.FALLBACK_UNKNOWN)

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    networks = trusted_proxy_networks(settings)
    if not networks:
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED,
        )

    if not is_trusted_proxy_address(peer, networks):
        if any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        ):
            _record_invalid_forwarding_telemetry(SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED)
        return ClientSourceResolution(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED,
        )

    cloudflare_networks = (
        parse_cidr_list(",".join(_CLOUDFLARE_EDGE_CIDRS))
        if settings.admin_trust_cloudflare_edge
        else ()
    )

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        parsed = _parse_forwarded_header(forwarded_header, networks)
        if parsed is not None:
            return ClientSourceResolution(
                source=parsed,
                path=SourceResolutionPath.TRUSTED_FORWARDED,
            )
        _record_invalid_forwarding_telemetry(SourceResolutionPath.INVALID_FORWARDING_DATA)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        parsed = _walk_x_forwarded_for(xff_header, networks)
        if parsed is not None:
            return ClientSourceResolution(
                source=parsed,
                path=SourceResolutionPath.TRUSTED_X_FORWARDED_FOR,
            )
        _record_invalid_forwarding_telemetry(SourceResolutionPath.INVALID_FORWARDING_DATA)

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and cloudflare_networks:
        if xff_header and _cloudflare_hop_present(xff_header, cloudflare_networks):
            normalized = normalize_ip_address(cf_header)
            if normalized is not None:
                return ClientSourceResolution(
                    source=normalized,
                    path=SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
                )
        _record_invalid_forwarding_telemetry(SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED)

    return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)


def client_source_for_request(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for limiter keying."""
    return resolve_admin_login_client_source(request, settings).source


def deployment_trust_flags(settings: Settings) -> dict[str, bool]:
    """Non-sensitive deployment verification flags (no raw CIDRs or addresses)."""
    return {
        "admin_trust_proxy_headers": settings.admin_trust_proxy_headers,
        "admin_trusted_proxy_cidrs_configured": bool(
            settings.admin_trusted_proxy_cidrs.strip()
        ),
        "admin_trust_cloudflare_edge": settings.admin_trust_cloudflare_edge,
        "uvicorn_forwarded_allow_ips_configured": bool(
            settings.uvicorn_forwarded_allow_ips.strip()
        ),
    }
