"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Mapping

_logger = logging.getLogger(__name__)

SOURCE_UNKNOWN = "unknown"
MAX_FORWARDING_CHAIN_LENGTH = 32

# Render private-network peers that terminate TLS before the app process.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "::1/128",
    "fc00::/7",
)

# Representative Cloudflare edge ranges for CF-Connecting-IP proof (override via env).
DEFAULT_CLOUDFLARE_EDGE_CIDRS = (
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

_UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS = 60.0
_untrusted_header_log_lock = Lock()
_untrusted_header_log_last_at = 0.0

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\"|\[[^\]]+\](?::\d+)?|[^;,\"]+))",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Telemetry-safe identifiers for how admin login source was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_NO_FORWARDING = "trusted_no_forwarding"
    XFF_RIGHT_TO_LEFT = "xff_right_to_left"
    FORWARDED_RIGHT_TO_LEFT = "forwarded_right_to_left"
    CF_CONNECTING_IP = "cf_connecting_ip"
    UNTRUSTED_HEADERS_IGNORED = "untrusted_headers_ignored"
    UNKNOWN_PEER = "unknown_peer"


@dataclass(frozen=True)
class SourceResolutionResult:
    source: str
    path: SourceResolutionPath
    had_forwarding_headers: bool = False


def parse_cidr_list(
    raw: str,
    *,
    defaults: tuple[str, ...] = (),
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs; ignore invalid entries."""
    spec = raw.strip()
    tokens = [part.strip() for part in spec.split(",") if part.strip()] if spec else list(defaults)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in tokens:
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Return canonical IPv4/IPv6 text without ports; map IPv4-mapped IPv6 to IPv4."""
    candidate = raw.strip()
    if not candidate or candidate.lower() == "unknown":
        return None

    host = candidate
    if host.startswith('"') and host.endswith('"'):
        host = host[1:-1].strip()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return None
        host = host[1:closing]
    elif host.count(":") == 1 and "." in host.split(":", 1)[0]:
        host = host.rsplit(":", 1)[0]

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def is_trusted_proxy_address(
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


def _split_forwarding_chain(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalize_hop_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> list[str]:
    normalized: list[str] = []
    for hop in hops[:MAX_FORWARDING_CHAIN_LENGTH]:
        address = normalize_ip_address(hop)
        if address is None:
            continue
        if normalized and normalized[-1] == address:
            continue
        normalized.append(address)
    if len(hops) > MAX_FORWARDING_CHAIN_LENGTH:
        _maybe_log_invalid_forwarding("chain_overlong")
    return normalized


def _client_from_trusted_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> str | None:
    if not hops:
        return None
    for address in reversed(hops):
        if is_trusted_proxy_address(address, trusted_networks):
            continue
        if cloudflare_networks and is_trusted_proxy_address(address, cloudflare_networks):
            continue
        return address
    return None


def _parse_forwarded_header_for_values(raw: str) -> list[str]:
    values: list[str] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        match = _FORWARDED_FOR_TOKEN.search(token)
        if match is None:
            continue
        value = match.group("value").strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if value.lower() == "_hidden":
            continue
        values.append(value)
        if len(values) >= MAX_FORWARDING_CHAIN_LENGTH:
            break
    return values


def _has_cloudflare_hop(
    hops: list[str],
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    return any(is_trusted_proxy_address(hop, cloudflare_networks) for hop in hops)


def _had_forwarding_headers(headers: Mapping[str, str]) -> bool:
    lowered = {key.lower(): value for key, value in headers.items()}
    return any(
        lowered.get(name, "").strip()
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _maybe_log_invalid_forwarding(reason: str) -> None:
    global _untrusted_header_log_last_at
    now = time.monotonic()
    with _untrusted_header_log_lock:
        if now - _untrusted_header_log_last_at < _UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS:
            return
        _untrusted_header_log_last_at = now
    _logger.info(
        "Admin login forwarding headers treated conservatively",
        extra={"forwarding_telemetry_reason": reason},
    )


def _maybe_log_untrusted_headers() -> None:
    _maybe_log_invalid_forwarding("untrusted_peer")


def emit_source_resolution_telemetry(result: SourceResolutionResult) -> None:
    _logger.info(
        "Admin login source resolved",
        extra={
            "source_resolution_path": result.path.value,
            "had_forwarding_headers": result.had_forwarding_headers,
        },
    )


def resolve_admin_login_client_source(
    *,
    peer_host: str | None,
    headers: Mapping[str, str],
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> SourceResolutionResult:
    """Resolve limiter source identity with verified trusted-proxy boundaries."""
    lowered = {key.lower(): value for key, value in headers.items()}
    had_forwarding = _had_forwarding_headers(headers)

    peer = normalize_ip_address(peer_host) if peer_host else None
    literal_peer = peer_host.strip() if peer_host else ""
    if peer is None:
        if literal_peer:
            if had_forwarding:
                _maybe_log_untrusted_headers()
            return SourceResolutionResult(
                source=literal_peer,
                path=(
                    SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED
                    if had_forwarding
                    else SourceResolutionPath.DIRECT_PEER
                ),
                had_forwarding_headers=had_forwarding,
            )
        if had_forwarding:
            _maybe_log_untrusted_headers()
        return SourceResolutionResult(
            source=SOURCE_UNKNOWN,
            path=SourceResolutionPath.UNKNOWN_PEER,
            had_forwarding_headers=had_forwarding,
        )

    peer_is_trusted = is_trusted_proxy_address(peer, trusted_proxy_networks)
    if not peer_is_trusted:
        if had_forwarding:
            _maybe_log_untrusted_headers()
        return SourceResolutionResult(
            source=peer,
            path=(
                SourceResolutionPath.UNTRUSTED_HEADERS_IGNORED
                if had_forwarding
                else SourceResolutionPath.DIRECT_PEER
            ),
            had_forwarding_headers=had_forwarding,
        )

    xff_raw = lowered.get("x-forwarded-for", "").strip()
    xff_hops = _normalize_hop_chain(
        _split_forwarding_chain(xff_raw),
        trusted_networks=trusted_proxy_networks,
    )

    forwarded_raw = lowered.get("forwarded", "").strip()
    forwarded_hops = _normalize_hop_chain(
        _parse_forwarded_header_for_values(forwarded_raw),
        trusted_networks=trusted_proxy_networks,
    )

    cf_connecting_ip = normalize_ip_address(lowered.get("cf-connecting-ip", ""))

    if xff_hops:
        client = _client_from_trusted_chain(
            xff_hops,
            trusted_networks=trusted_proxy_networks,
            cloudflare_networks=cloudflare_edge_networks,
        )
        if client is not None:
            if (
                cf_connecting_ip is not None
                and _has_cloudflare_hop(xff_hops, cloudflare_networks=cloudflare_edge_networks)
                and cf_connecting_ip == client
            ):
                return SourceResolutionResult(
                    source=cf_connecting_ip,
                    path=SourceResolutionPath.CF_CONNECTING_IP,
                    had_forwarding_headers=had_forwarding,
                )
            return SourceResolutionResult(
                source=client,
                path=SourceResolutionPath.XFF_RIGHT_TO_LEFT,
                had_forwarding_headers=had_forwarding,
            )
        _maybe_log_invalid_forwarding("xff_all_trusted")

    if forwarded_hops:
        client = _client_from_trusted_chain(
            forwarded_hops,
            trusted_networks=trusted_proxy_networks,
            cloudflare_networks=cloudflare_edge_networks,
        )
        if client is not None:
            return SourceResolutionResult(
                source=client,
                path=SourceResolutionPath.FORWARDED_RIGHT_TO_LEFT,
                had_forwarding_headers=had_forwarding,
            )
        _maybe_log_invalid_forwarding("forwarded_all_trusted")

    if (
        cf_connecting_ip is not None
        and xff_hops
        and _has_cloudflare_hop(xff_hops, cloudflare_networks=cloudflare_edge_networks)
    ):
        return SourceResolutionResult(
            source=cf_connecting_ip,
            path=SourceResolutionPath.CF_CONNECTING_IP,
            had_forwarding_headers=had_forwarding,
        )

    if had_forwarding:
        _maybe_log_invalid_forwarding("trusted_peer_no_client")

    return SourceResolutionResult(
        source=SOURCE_UNKNOWN,
        path=SourceResolutionPath.TRUSTED_NO_FORWARDING,
        had_forwarding_headers=had_forwarding,
    )
