"""Trusted-proxy client source resolution for admin login rate limiting."""

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

# Immediate peer must match one of these networks before forwarding headers apply.
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

DEFAULT_TRUSTED_FORWARDING_CIDRS: tuple[str, ...] = DEFAULT_TRUSTED_PROXY_CIDRS + (
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

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/).
DEFAULT_CLOUDFLARE_FORWARDING_CIDRS: tuple[str, ...] = tuple(
    cidr for cidr in DEFAULT_TRUSTED_FORWARDING_CIDRS if cidr not in DEFAULT_TRUSTED_PROXY_CIDRS
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNKNOWN_SOURCE = "unknown"

# Resolution paths surfaced in structured logs (no raw addresses).
RESOLUTION_DIRECT_PEER = "direct_peer"
RESOLUTION_TRUSTED_X_FORWARDED_FOR = "trusted_x_forwarded_for"
RESOLUTION_TRUSTED_FORWARDED = "trusted_forwarded"
RESOLUTION_TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
RESOLUTION_TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
RESOLUTION_UNTRUSTED_HEADERS_IGNORED = "untrusted_headers_ignored"
RESOLUTION_INVALID_FORWARDING_DATA = "invalid_forwarding_data"

_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_telemetry_lock = Lock()
_last_untrusted_log_at = 0.0
_untrusted_attempt_count = 0

_FORWARDED_FOR_TOKEN = re.compile(
    r"^\s*for=(?:\"([^\"]+)\"|([^;,]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TrustedNetworks:
    """Parsed trusted IP literals and networks."""

    hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]
    literals: frozenset[str]

    @classmethod
    def from_cidrs(cls, cidrs: Iterable[str]) -> TrustedNetworks:
        hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
        literals: set[str] = set()
        for raw in cidrs:
            item = raw.strip()
            if not item:
                continue
            if "/" in item:
                try:
                    networks.add(ipaddress.ip_network(item, strict=False))
                except ValueError:
                    literals.add(item)
            else:
                try:
                    hosts.add(ipaddress.ip_address(item))
                except ValueError:
                    literals.add(item)
        return cls(frozenset(hosts), frozenset(networks), frozenset(literals))

    def contains(self, host: str) -> bool:
        if not host:
            return False
        if host in self.literals:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip in self.hosts:
            return True
        return any(ip in network for network in self.networks)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and observability metadata."""

    source: str
    path: str
    untrusted_forwarding_attempt: bool = False


def parse_cidr_list(raw: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse comma-separated CIDRs; fall back to defaults when unset."""
    if not raw.strip():
        return default
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts or default


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 strings deterministically; strip ports and brackets."""
    value = raw.strip()
    if not value:
        return None
    if value.lower() == "unknown":
        return _UNKNOWN_SOURCE

    host = value
    if host.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder.startswith(":"):
            port_part = remainder[1:]
            if not port_part.isdigit():
                return None
    elif host.count(":") == 1 and "." in host:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part
    elif host.count(":") == 1 and "." not in host:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _split_forwarding_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _client_from_x_forwarded_for(
    header_value: str,
    *,
    forwarding_trusted: TrustedNetworks,
) -> str | None:
    chain = _split_forwarding_chain(header_value)
    if not chain:
        return None
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for entry in reversed(chain):
        host = entry
        if host.startswith("["):
            closing = host.find("]")
            if closing == -1:
                return None
            host = host[1:closing]
        elif host.count(":") == 1 and "." in host:
            host, port = host.rsplit(":", 1)
            if not port.isdigit():
                return None

        normalized = normalize_ip_address(host)
        if normalized is None:
            return None
        if not forwarding_trusted.contains(normalized):
            return normalized
    return None


def _client_from_forwarded_header(
    header_value: str,
    *,
    forwarding_trusted: TrustedNetworks,
) -> str | None:
    candidates: list[str] = []
    for segment in header_value.split(","):
        segment = segment.strip()
        if not segment:
            continue
        match = _FORWARDED_FOR_TOKEN.match(segment)
        if not match:
            continue
        token = (match.group(1) or match.group(2) or "").strip()
        if token.lower() == "unknown":
            candidates.append(_UNKNOWN_SOURCE)
            continue
        if token.startswith("_"):
            continue
        candidates.append(token)
        if len(candidates) > MAX_FORWARDING_CHAIN_LENGTH:
            return None

    if not candidates:
        return None

    for entry in reversed(candidates):
        normalized = normalize_ip_address(entry)
        if normalized is None:
            return None
        if not forwarding_trusted.contains(normalized):
            return normalized
    return None


def _chain_contains_cloudflare_hop(
    header_value: str | None,
    *,
    cloudflare_trusted: TrustedNetworks,
) -> bool:
    if not header_value:
        return False
    for entry in _split_forwarding_chain(header_value):
        normalized = normalize_ip_address(entry)
        if normalized and cloudflare_trusted.contains(normalized):
            return True
    return False


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host or None


def _record_untrusted_forwarding_attempt() -> None:
    global _last_untrusted_log_at, _untrusted_attempt_count
    now = time.monotonic()
    with _telemetry_lock:
        _untrusted_attempt_count += 1
        if now - _last_untrusted_log_at < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            return
        sampled_count = _untrusted_attempt_count
        _untrusted_attempt_count = 0
        _last_untrusted_log_at = now
    _logger.info(
        "Admin login source ignored untrusted forwarding headers",
        extra={
            "source_resolution_path": RESOLUTION_UNTRUSTED_HEADERS_IGNORED,
            "sampled_untrusted_forwarding_attempts": sampled_count,
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain: public client → Cloudflare edge → Render load balancer → Uvicorn.

    Forwarding headers are honored only when the immediate TCP peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. Client identity is derived by walking
    ``X-Forwarded-For`` / ``Forwarded`` right-to-left and skipping hops in
    ``ADMIN_TRUSTED_FORWARDING_CIDRS``. ``CF-Connecting-IP`` is used only as a
    fallback when the peer is trusted and the XFF chain contains a Cloudflare hop.
    """
    proxy_boundary = TrustedNetworks.from_cidrs(settings.admin_trusted_proxy_cidrs)
    forwarding_trusted = TrustedNetworks.from_cidrs(settings.admin_trusted_forwarding_cidrs)
    cloudflare_trusted = TrustedNetworks.from_cidrs(
        settings.admin_cloudflare_forwarding_cidrs
    )

    peer_raw = _immediate_peer_host(request)
    if peer_raw is None:
        return ClientSourceResolution(_UNKNOWN_SOURCE, RESOLUTION_DIRECT_PEER)

    peer = normalize_ip_address(peer_raw)
    if peer is None:
        peer = peer_raw.strip().lower()
    if not peer:
        return ClientSourceResolution(_UNKNOWN_SOURCE, RESOLUTION_DIRECT_PEER)

    forwarding_headers_present = any(
        request.headers.get(name)
        for name in (
            "x-forwarded-for",
            "forwarded",
            "cf-connecting-ip",
        )
    )

    if not settings.admin_trusted_proxy_cidrs or not proxy_boundary.contains(peer):
        if forwarding_headers_present:
            _record_untrusted_forwarding_attempt()
            return ClientSourceResolution(
                peer,
                RESOLUTION_UNTRUSTED_HEADERS_IGNORED,
                untrusted_forwarding_attempt=True,
            )
        return ClientSourceResolution(peer, RESOLUTION_DIRECT_PEER)

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    forwarded = request.headers.get("forwarded", "")
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")

    if x_forwarded_for:
        client = _client_from_x_forwarded_for(
            x_forwarded_for,
            forwarding_trusted=forwarding_trusted,
        )
        if client is not None:
            return ClientSourceResolution(client, RESOLUTION_TRUSTED_X_FORWARDED_FOR)

        if cf_connecting_ip and _chain_contains_cloudflare_hop(
            x_forwarded_for,
            cloudflare_trusted=cloudflare_trusted,
        ):
            client = normalize_ip_address(cf_connecting_ip)
            if client is not None:
                return ClientSourceResolution(client, RESOLUTION_TRUSTED_CF_CONNECTING_IP)

        if forwarded:
            client = _client_from_forwarded_header(
                forwarded,
                forwarding_trusted=forwarding_trusted,
            )
            if client is not None:
                return ClientSourceResolution(client, RESOLUTION_TRUSTED_FORWARDED)

        if x_forwarded_for or forwarded or cf_connecting_ip:
            return ClientSourceResolution(peer, RESOLUTION_INVALID_FORWARDING_DATA)

    if forwarded:
        client = _client_from_forwarded_header(
            forwarded,
            forwarding_trusted=forwarding_trusted,
        )
        if client is not None:
            return ClientSourceResolution(client, RESOLUTION_TRUSTED_FORWARDED)
        return ClientSourceResolution(peer, RESOLUTION_INVALID_FORWARDING_DATA)

    if cf_connecting_ip:
        return ClientSourceResolution(peer, RESOLUTION_INVALID_FORWARDING_DATA)

    if forwarding_headers_present:
        return ClientSourceResolution(peer, RESOLUTION_INVALID_FORWARDING_DATA)

    return ClientSourceResolution(peer, RESOLUTION_TRUSTED_PEER_FALLBACK)


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _last_untrusted_log_at, _untrusted_attempt_count
    with _telemetry_lock:
        _last_untrusted_log_at = 0.0
        _untrusted_attempt_count = 0
