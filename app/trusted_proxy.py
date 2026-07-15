"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Mapping

_logger = logging.getLogger(__name__)

MISSING_CLIENT_SOURCE = "unknown"
MAX_FORWARDING_CHAIN_LENGTH = 32
_CLOUDFLARE_EDGE_HEADER = "cf-ray"
_CF_CONNECTING_IP_HEADER = "cf-connecting-ip"
_X_FORWARDED_FOR_HEADER = "x-forwarded-for"
_FORWARDED_HEADER = "forwarded"

# Published Cloudflare IPv4 ranges (https://www.cloudflare.com/ips-v4).
_DEFAULT_CLOUDFLARE_EDGE_CIDRS: tuple[str, ...] = (
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

# Render and other private-network load balancers in front of the app process.
_DEFAULT_RENDER_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
)

_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_invalid_forwarding_lock = threading.Lock()
_last_invalid_forwarding_log_at = 0.0

_FORWARDED_FOR_VALUE = re.compile(
    r'for="?(?:(?P<ipv6>\[[0-9a-fA-F:.]+\])|(?P<ipv4>[\d.]+))"?(?:;|,|\s|$)',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and telemetry-safe metadata."""

    source: str
    path: str
    rejected_forwarding: bool = False


def default_trusted_proxy_cidrs() -> tuple[str, ...]:
    """Return the version-controlled production proxy boundary."""
    return _DEFAULT_RENDER_PROXY_CIDRS + _DEFAULT_CLOUDFLARE_EDGE_CIDRS


def parse_trusted_networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 without port suffixes."""
    candidate = raw.strip().strip('"').strip("'")
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        host, _, port = candidate[1:].partition("]")
        if port.startswith(":"):
            port = port[1:]
        candidate = host
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
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


def is_trusted_address(address: str, trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _parse_forwarded_for_chain(header_value: str) -> list[str]:
    if len(header_value) > 2048:
        return []
    parts = [segment.strip() for segment in header_value.split(",")]
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return []
    chain: list[str] = []
    for part in parts:
        if not part:
            continue
        normalized = normalize_ip_address(part)
        if normalized is None:
            return []
        chain.append(normalized)
    return chain


def _parse_forwarded_header(header_value: str) -> list[str]:
    if len(header_value) > 4096:
        return []
    chain: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_VALUE.search(entry)
        if match is None:
            continue
        raw_host = match.group("ipv6") or match.group("ipv4")
        if raw_host is None:
            continue
        normalized = normalize_ip_address(raw_host.strip("[]"))
        if normalized is None:
            return []
        chain.append(normalized)
        if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
            return []
    return chain


def _walk_trusted_forwarding_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if not hops:
        return None
    for hop in reversed(hops):
        if is_trusted_address(hop, trusted_networks):
            continue
        return hop
    return hops[0]


def _cloudflare_edge_proven(headers: Mapping[str, str]) -> bool:
    return bool(headers.get(_CLOUDFLARE_EDGE_HEADER, "").strip())


def _maybe_log_rejected_forwarding(path: str) -> None:
    global _last_invalid_forwarding_log_at
    now = time.monotonic()
    with _invalid_forwarding_lock:
        if now - _last_invalid_forwarding_log_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _last_invalid_forwarding_log_at = now
    _logger.info(
        "Admin login source resolution rejected forwarding headers",
        extra={"source_resolution_path": path},
    )


def resolve_client_source(
    *,
    peer_host: str | None,
    headers: Mapping[str, str],
    trust_proxy_headers: bool,
    trusted_proxy_cidrs: tuple[str, ...],
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source from peer + forwarding headers."""
    normalized_peer = normalize_ip_address(peer_host or "")
    if normalized_peer is None:
        return ClientSourceResolution(
            source=MISSING_CLIENT_SOURCE,
            path="missing_peer",
        )

    trusted_networks = parse_trusted_networks(
        trusted_proxy_cidrs if trusted_proxy_cidrs else default_trusted_proxy_cidrs()
    )
    peer_trusted = is_trusted_address(normalized_peer, trusted_networks)
    lowered_headers = {key.lower(): value for key, value in headers.items()}

    if not trust_proxy_headers or not peer_trusted:
        if trust_proxy_headers and not peer_trusted and _forwarding_headers_present(lowered_headers):
            _maybe_log_rejected_forwarding("untrusted_peer_forwarding")
            return ClientSourceResolution(
                source=normalized_peer,
                path="direct_peer",
                rejected_forwarding=True,
            )
        return ClientSourceResolution(
            source=normalized_peer,
            path="direct_peer",
        )

    cf_connecting_ip = lowered_headers.get(_CF_CONNECTING_IP_HEADER, "").strip()
    if cf_connecting_ip and _cloudflare_edge_proven(lowered_headers):
        normalized_cf = normalize_ip_address(cf_connecting_ip)
        if normalized_cf is not None:
            return ClientSourceResolution(
                source=normalized_cf,
                path="cf_connecting_ip",
            )
        _maybe_log_rejected_forwarding("malformed_cf_connecting_ip")
        return ClientSourceResolution(
            source=normalized_peer,
            path="malformed_forwarding",
            rejected_forwarding=True,
        )

    if cf_connecting_ip and not _cloudflare_edge_proven(lowered_headers):
        _maybe_log_rejected_forwarding("unproven_cf_connecting_ip")

    xff_chain = _parse_forwarded_for_chain(lowered_headers.get(_X_FORWARDED_FOR_HEADER, ""))
    if xff_chain:
        hops = xff_chain + [normalized_peer]
        client = _walk_trusted_forwarding_chain(hops, trusted_networks=trusted_networks)
        if client is not None:
            return ClientSourceResolution(
                source=client,
                path="xff_trusted_walk",
            )
        _maybe_log_rejected_forwarding("malformed_x_forwarded_for")
        return ClientSourceResolution(
            source=normalized_peer,
            path="malformed_forwarding",
            rejected_forwarding=True,
        )

    if lowered_headers.get(_X_FORWARDED_FOR_HEADER, "").strip() and not xff_chain:
        _maybe_log_rejected_forwarding("malformed_x_forwarded_for")
        return ClientSourceResolution(
            source=normalized_peer,
            path="malformed_forwarding",
            rejected_forwarding=True,
        )

    forwarded_chain = _parse_forwarded_header(lowered_headers.get(_FORWARDED_HEADER, ""))
    if lowered_headers.get(_FORWARDED_HEADER, "").strip():
        if forwarded_chain:
            hops = forwarded_chain + [normalized_peer]
            client = _walk_trusted_forwarding_chain(hops, trusted_networks=trusted_networks)
            if client is not None:
                return ClientSourceResolution(
                    source=client,
                    path="forwarded_trusted_walk",
                )
        _maybe_log_rejected_forwarding("malformed_forwarded")
        return ClientSourceResolution(
            source=normalized_peer,
            path="malformed_forwarding",
            rejected_forwarding=True,
        )

    if _forwarding_headers_present(lowered_headers):
        _maybe_log_rejected_forwarding("ignored_forwarding_without_chain")
        return ClientSourceResolution(
            source=normalized_peer,
            path="trusted_peer_only",
            rejected_forwarding=True,
        )

    return ClientSourceResolution(
        source=normalized_peer,
        path="trusted_peer_only",
    )


def _forwarding_headers_present(headers: Mapping[str, str]) -> bool:
    return any(
        headers.get(name, "").strip()
        for name in (
            _X_FORWARDED_FOR_HEADER,
            _FORWARDED_HEADER,
            _CF_CONNECTING_IP_HEADER,
        )
    )


def reset_source_resolution_telemetry() -> None:
    """Clear rate-limited telemetry state (tests only)."""
    global _last_invalid_forwarding_log_at
    with _invalid_forwarding_lock:
        _last_invalid_forwarding_log_at = 0.0
