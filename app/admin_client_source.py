"""Trusted-proxy client source resolution for admin login rate limiting.

Production request chain (public edge → application process):

    Client → Cloudflare (edge) → Render load balancer → Uvicorn

Forwarding headers are honored only after the immediate TCP peer is verified
against ``ADMIN_TRUSTED_PROXY_CIDRS`` (or the ``cloudflare-render`` preset).
Untrusted peers always resolve to the direct peer address; spoofed
``X-Forwarded-For``, ``Forwarded``, and ``CF-Connecting-IP`` values are ignored.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock

from fastapi import Request

from app.config import Settings

MAX_FORWARD_CHAIN_LENGTH = 32
UNKNOWN_SOURCE = "unknown"
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0

# Render's in-platform proxy connects from loopback / private space.
RENDER_PLATFORM_CIDRS: tuple[str, ...] = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
)

# Cloudflare published edge ranges (https://www.cloudflare.com/ips-v4 / ips-v6).
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

_logger = logging.getLogger(__name__)
_telemetry_lock = Lock()
_last_untrusted_telemetry_at = 0.0


class ClientSourcePath(str, Enum):
    """Bounded resolution path labels for operational telemetry (no raw IPs)."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_WALK = "trusted_xff_walk"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    UNTRUSTED_FORWARDING_IGNORED = "untrusted_forwarding_ignored"
    MALFORMED_FORWARDING = "malformed_forwarding"
    OVERLONG_CHAIN = "overlong_chain"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved limiter source identity and the algorithm path used."""

    source: str
    path: ClientSourcePath


def reset_client_source_telemetry_for_tests() -> None:
    """Clear rate-limited telemetry state (tests only)."""
    global _last_untrusted_telemetry_at
    with _telemetry_lock:
        _last_untrusted_telemetry_at = 0.0


def resolve_trusted_proxy_cidr_strings(settings: Settings) -> tuple[str, ...]:
    """Return the configured trusted-proxy CIDR strings for this deployment."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    preset = settings.admin_trusted_proxy_preset.strip().lower()
    if preset == "cloudflare-render":
        return RENDER_PLATFORM_CIDRS + CLOUDFLARE_EDGE_CIDRS
    return ()


def parse_trusted_proxy_networks(
    cidr_strings: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidr_strings:
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            _logger.warning(
                "Ignoring invalid trusted proxy CIDR entry",
                extra={"cidr_entry_length": len(candidate)},
            )
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject invalid input."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host_part, _, port_part = candidate[1:].partition("]")
        if port_part.startswith(":"):
            if not port_part[1:].isdigit():
                return None
        elif port_part:
            return None
        candidate = host_part
    elif candidate.count(":") == 1 and "." in candidate:
        host_part, port_part = candidate.rsplit(":", 1)
        if not port_part.isdigit():
            return None
        candidate = host_part

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def _ip_in_trusted_networks(
    ip_str: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(ip_str)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _parse_x_forwarded_for(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


_FORWARDED_FOR_RE = re.compile(
    r"""for=(?:\s*"([^"]+)"|\s*([^;\s]+))""",
    re.IGNORECASE,
)


def _parse_forwarded_header(header_value: str) -> list[str]:
    addresses: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        raw = match.group(1) or match.group(2) or ""
        if raw.lower() == "unknown":
            continue
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1]
            if inner.rsplit(":", 1)[-1].isdigit() and inner.count(":") > 1:
                raw = inner.rsplit(":", 1)[0]
            else:
                raw = inner
        normalized = normalize_ip_address(raw)
        if normalized is not None:
            addresses.append(normalized)
    return addresses


def _walk_trusted_xff_chain(
    hops: list[str],
    *,
    peer_ip: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Right-to-left trusted-hop walk; return first untrusted hop."""
    if len(hops) > MAX_FORWARD_CHAIN_LENGTH:
        return None

    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    if normalized_hops and normalized_hops[-1] != peer_ip:
        normalized_hops.append(peer_ip)

    for hop in reversed(normalized_hops):
        if not _ip_in_trusted_networks(hop, trusted_networks):
            return hop
    return normalize_ip_address(peer_ip)


def _cloudflare_edge_verified(
    *,
    peer_ip: str,
    xff_hops: list[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if _ip_in_trusted_networks(peer_ip, cloudflare_networks):
        return True
    for hop in xff_hops:
        if _ip_in_trusted_networks(hop, cloudflare_networks):
            return True
    if not cloudflare_networks:
        return False
    # Peer must be a trusted Render/platform hop with a Cloudflare hop in XFF.
    if not _ip_in_trusted_networks(peer_ip, trusted_networks):
        return False
    return any(_ip_in_trusted_networks(hop, cloudflare_networks) for hop in xff_hops)


def _emit_client_source_telemetry(result: ClientSourceResult) -> None:
    path = result.path.value
    if result.path in {
        ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED,
        ClientSourcePath.MALFORMED_FORWARDING,
        ClientSourcePath.OVERLONG_CHAIN,
    }:
        global _last_untrusted_telemetry_at
        now = time.monotonic()
        with _telemetry_lock:
            if now - _last_untrusted_telemetry_at < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
                return
            _last_untrusted_telemetry_at = now
        _logger.info(
            "Admin login forwarding headers ignored or invalid",
            extra={"client_source_path": path},
        )
        return

    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_path": path},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
    *,
    emit_telemetry: bool = True,
) -> ClientSourceResult:
    """Resolve the effective client source for admin login rate limiting."""
    if request.client is None or not request.client.host:
        result = ClientSourceResult(source=UNKNOWN_SOURCE, path=ClientSourcePath.MISSING_PEER)
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    peer_ip = normalize_ip_address(request.client.host)
    if peer_ip is None:
        raw_host = request.client.host.strip()
        if not raw_host or len(raw_host) > 253:
            result = ClientSourceResult(
                source=UNKNOWN_SOURCE,
                path=ClientSourcePath.MALFORMED_FORWARDING,
            )
            if emit_telemetry:
                _emit_client_source_telemetry(result)
            return result
        peer_ip = raw_host

    cidr_strings = resolve_trusted_proxy_cidr_strings(settings)
    trusted_networks = parse_trusted_proxy_networks(cidr_strings)
    cloudflare_networks = parse_trusted_proxy_networks(CLOUDFLARE_EDGE_CIDRS)

    has_forwarding_headers = any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )

    if not trusted_networks:
        path = (
            ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED
            if has_forwarding_headers
            else ClientSourcePath.DIRECT_PEER
        )
        result = ClientSourceResult(source=peer_ip, path=path)
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    if not _ip_in_trusted_networks(peer_ip, trusted_networks):
        path = (
            ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED
            if has_forwarding_headers
            else ClientSourcePath.DIRECT_PEER
        )
        result = ClientSourceResult(source=peer_ip, path=path)
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_hops = _parse_x_forwarded_for(xff_header) if xff_header else []

    if len(xff_hops) > MAX_FORWARD_CHAIN_LENGTH:
        result = ClientSourceResult(source=peer_ip, path=ClientSourcePath.OVERLONG_CHAIN)
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and _cloudflare_edge_verified(
        peer_ip=peer_ip,
        xff_hops=xff_hops,
        cloudflare_networks=cloudflare_networks,
        trusted_networks=trusted_networks,
    ):
        cf_ip = normalize_ip_address(cf_header)
        if cf_ip is not None:
            result = ClientSourceResult(source=cf_ip, path=ClientSourcePath.CF_CONNECTING_IP)
            if emit_telemetry:
                _emit_client_source_telemetry(result)
            return result

    if xff_hops:
        walked = _walk_trusted_xff_chain(
            xff_hops,
            peer_ip=peer_ip,
            trusted_networks=trusted_networks,
        )
        if walked is None:
            result = ClientSourceResult(
                source=peer_ip,
                path=ClientSourcePath.MALFORMED_FORWARDING,
            )
        else:
            result = ClientSourceResult(source=walked, path=ClientSourcePath.TRUSTED_XFF_WALK)
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_hops = _parse_forwarded_header(forwarded_header)
        if len(forwarded_hops) > MAX_FORWARD_CHAIN_LENGTH:
            result = ClientSourceResult(source=peer_ip, path=ClientSourcePath.OVERLONG_CHAIN)
        elif not forwarded_hops:
            result = ClientSourceResult(
                source=peer_ip,
                path=ClientSourcePath.MALFORMED_FORWARDING,
            )
        else:
            walked = _walk_trusted_xff_chain(
                forwarded_hops,
                peer_ip=peer_ip,
                trusted_networks=trusted_networks,
            )
            if walked is None:
                result = ClientSourceResult(
                    source=peer_ip,
                    path=ClientSourcePath.MALFORMED_FORWARDING,
                )
            else:
                result = ClientSourceResult(
                    source=walked,
                    path=ClientSourcePath.FORWARDED_HEADER,
                )
        if emit_telemetry:
            _emit_client_source_telemetry(result)
        return result

    result = ClientSourceResult(source=peer_ip, path=ClientSourcePath.DIRECT_PEER)
    if emit_telemetry:
        _emit_client_source_telemetry(result)
    return result
