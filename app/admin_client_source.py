"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

UNKNOWN_PEER = "unknown"
AMBIGUOUS_FORWARDING_SENTINEL = "unverified-forwarded"
MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_INVALID_FORWARDING_LOG_BURST = 5

_logger = logging.getLogger(__name__)
_invalid_forwarding_log_state: dict[str, tuple[int, float]] = {}


class ClientSourcePath(StrEnum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    UNKNOWN_PEER = "unknown_peer"
    AMBIGUOUS_FORWARDING = "ambiguous_forwarding"


# Render/internal hops in the Cloudflare → Render → Uvicorn chain.
_DEFAULT_INTERNAL_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

# Published Cloudflare origin-connect ranges (https://www.cloudflare.com/ips/).
_CLOUDFLARE_CIDRS: tuple[str, ...] = (
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

_FORWARDED_FOR_TOKEN_RE = re.compile(r"^for=(?P<value>[^;]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    address: str
    path: ClientSourcePath


class TrustedProxyNetworks:
    """Trusted immediate peers and forwarding hops for admin login source."""

    def __init__(self, entries: Iterable[str]) -> None:
        self._hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._literals: set[str] = set()
        self._cloudflare_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        for entry in entries:
            normalized = entry.strip()
            if not normalized:
                continue
            if "/" in normalized:
                try:
                    network = ipaddress.ip_network(normalized, strict=False)
                except ValueError:
                    self._literals.add(normalized)
                    continue
                self._networks.append(network)
                continue
            try:
                self._hosts.add(ipaddress.ip_address(normalized))
            except ValueError:
                self._literals.add(normalized)

        for entry in _CLOUDFLARE_CIDRS:
            self._cloudflare_networks.append(ipaddress.ip_network(entry, strict=False))

        self._contains = functools.lru_cache(maxsize=4096)(self._compute_contains)

    def contains(self, host: str | None) -> bool:
        if not host:
            return False
        if len(host) > 253:
            return self._compute_contains(host)
        return self._contains(host)

    def _compute_contains(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host in self._literals
        if ip in self._hosts:
            return True
        return any(ip in network for network in self._networks)

    def cloudflare_hop_present(self, chain: tuple[str, ...]) -> bool:
        if len(chain) < 2:
            return False
        for host in chain[1:]:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                continue
            if any(ip in network for network in self._cloudflare_networks):
                return True
        return False


def default_trusted_proxy_entries(extra_cidrs: str = "") -> tuple[str, ...]:
    """Return the default trusted-proxy allowlist for production proxy chains."""
    entries = list(_DEFAULT_INTERNAL_CIDRS)
    entries.extend(_CLOUDFLARE_CIDRS)
    if extra_cidrs.strip():
        entries.extend(item.strip() for item in extra_cidrs.split(",") if item.strip())
    return tuple(dict.fromkeys(entries))


def trusted_proxy_networks(settings: Settings) -> TrustedProxyNetworks:
    return TrustedProxyNetworks(default_trusted_proxy_entries(settings.admin_trusted_proxy_cidrs))


def normalize_client_address(raw_value: str) -> str | None:
    """Normalize one forwarded or peer address for deterministic limiter keys."""
    candidate = raw_value.strip().strip('"')
    if not candidate or len(candidate) > 253:
        return None

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":"):
            try:
                int(remainder[1:])
            except ValueError:
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        try:
            int(port)
        except ValueError:
            host = candidate
    else:
        host = candidate

    host = host.strip()
    if not host:
        return None

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.compressed
    return str(ip)


def _parse_host_port(token: str) -> tuple[str, int]:
    value = token.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return value, 0
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder.startswith(":"):
            try:
                return host, int(remainder[1:])
            except ValueError:
                return host, 0
        return host, 0
    if value.count(":") == 1 and "." in value:
        host, _, port_text = value.partition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return value, 0
    return value, 0


def _split_forwarded_for(header_value: str) -> tuple[str, ...]:
    if not header_value or len(header_value) > 4096:
        return ()
    tokens = [item.strip() for item in header_value.split(",")]
    if len(tokens) > MAX_FORWARDED_CHAIN_LENGTH:
        return ()
    normalized: list[str] = []
    for token in tokens:
        if not token:
            return ()
        host, _port = _parse_host_port(token)
        address = normalize_client_address(host)
        if address is None:
            return ()
        normalized.append(address)
    return tuple(normalized)


def _parse_forwarded_header(header_value: str) -> tuple[str, ...]:
    if not header_value or len(header_value) > 4096:
        return ()
    entries = [item.strip() for item in header_value.split(",") if item.strip()]
    if not entries or len(entries) > MAX_FORWARDED_CHAIN_LENGTH:
        return ()
    chain: list[str] = []
    for entry in entries:
        match = _FORWARDED_FOR_TOKEN_RE.match(entry)
        if match is None:
            return ()
        value = match.group("value").strip().strip('"')
        if value.lower() == "unknown":
            return ()
        address = normalize_client_address(value)
        if address is None:
            return ()
        chain.append(address)
    return tuple(chain)


def _resolve_from_trusted_chain(
    chain: tuple[str, ...],
    trusted: TrustedProxyNetworks,
) -> str | None:
    if not chain:
        return None
    if len(chain) == 1:
        return None
    for host in reversed(chain):
        if not trusted.contains(host):
            return host
    return chain[0]


def _log_invalid_forwarding(path: ClientSourcePath) -> None:
    now = time.monotonic()
    bucket = path.value
    count, window_start = _invalid_forwarding_log_state.get(bucket, (0, now))
    if now - window_start >= _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
        count = 0
        window_start = now
    count += 1
    _invalid_forwarding_log_state[bucket] = (count, window_start)
    if count <= _INVALID_FORWARDING_LOG_BURST:
        _logger.info(
            "Admin login client source rejected forwarded identity",
            extra={"client_source_path": path.value, "sampled": count > 1},
        )


def _emit_resolution_telemetry(path: ClientSourcePath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_host = request.client.host if request.client is not None else None
    peer = normalize_client_address(peer_host) if peer_host else None
    direct_peer = peer if peer is not None else (peer_host.strip() if peer_host else None)

    if not settings.admin_trust_proxy_headers:
        if direct_peer is None:
            _emit_resolution_telemetry(ClientSourcePath.UNKNOWN_PEER)
            return ClientSourceResolution(UNKNOWN_PEER, ClientSourcePath.UNKNOWN_PEER)
        _emit_resolution_telemetry(ClientSourcePath.DIRECT_PEER)
        return ClientSourceResolution(direct_peer, ClientSourcePath.DIRECT_PEER)

    trusted = trusted_proxy_networks(settings)
    if peer is None or not trusted.contains(peer):
        if direct_peer is None:
            _emit_resolution_telemetry(ClientSourcePath.UNKNOWN_PEER)
            return ClientSourceResolution(UNKNOWN_PEER, ClientSourcePath.UNKNOWN_PEER)
        _emit_resolution_telemetry(ClientSourcePath.DIRECT_PEER)
        return ClientSourceResolution(direct_peer, ClientSourcePath.DIRECT_PEER)

    xff_chain = _split_forwarded_for(request.headers.get("x-forwarded-for", ""))
    if xff_chain:
        resolved = _resolve_from_trusted_chain(xff_chain, trusted)
        if resolved is not None:
            _emit_resolution_telemetry(ClientSourcePath.FORWARDED_CHAIN)
            return ClientSourceResolution(resolved, ClientSourcePath.FORWARDED_CHAIN)
        if len(xff_chain) == 1:
            _log_invalid_forwarding(ClientSourcePath.AMBIGUOUS_FORWARDING)
            _emit_resolution_telemetry(ClientSourcePath.AMBIGUOUS_FORWARDING)
            return ClientSourceResolution(
                AMBIGUOUS_FORWARDING_SENTINEL,
                ClientSourcePath.AMBIGUOUS_FORWARDING,
            )
        cf_connecting_ip = normalize_client_address(
            request.headers.get("cf-connecting-ip", "")
        )
        if cf_connecting_ip is not None and trusted.cloudflare_hop_present(xff_chain):
            _emit_resolution_telemetry(ClientSourcePath.CF_CONNECTING_IP)
            return ClientSourceResolution(
                cf_connecting_ip,
                ClientSourcePath.CF_CONNECTING_IP,
            )
        _log_invalid_forwarding(ClientSourcePath.AMBIGUOUS_FORWARDING)
        _emit_resolution_telemetry(ClientSourcePath.AMBIGUOUS_FORWARDING)
        return ClientSourceResolution(
            AMBIGUOUS_FORWARDING_SENTINEL,
            ClientSourcePath.AMBIGUOUS_FORWARDING,
        )

    cf_connecting_ip = normalize_client_address(request.headers.get("cf-connecting-ip", ""))
    if cf_connecting_ip is not None:
        _log_invalid_forwarding(ClientSourcePath.AMBIGUOUS_FORWARDING)
        _emit_resolution_telemetry(ClientSourcePath.AMBIGUOUS_FORWARDING)
        return ClientSourceResolution(
            AMBIGUOUS_FORWARDING_SENTINEL,
            ClientSourcePath.AMBIGUOUS_FORWARDING,
        )

    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))
    if forwarded_chain:
        resolved = _resolve_from_trusted_chain(forwarded_chain, trusted)
        if resolved is not None:
            _emit_resolution_telemetry(ClientSourcePath.FORWARDED_HEADER)
            return ClientSourceResolution(resolved, ClientSourcePath.FORWARDED_HEADER)
        _log_invalid_forwarding(ClientSourcePath.AMBIGUOUS_FORWARDING)
        _emit_resolution_telemetry(ClientSourcePath.AMBIGUOUS_FORWARDING)
        return ClientSourceResolution(
            AMBIGUOUS_FORWARDING_SENTINEL,
            ClientSourcePath.AMBIGUOUS_FORWARDING,
        )

    if peer is None:
        _emit_resolution_telemetry(ClientSourcePath.UNKNOWN_PEER)
        return ClientSourceResolution(UNKNOWN_PEER, ClientSourcePath.UNKNOWN_PEER)
    _emit_resolution_telemetry(ClientSourcePath.DIRECT_PEER)
    return ClientSourceResolution(peer, ClientSourcePath.DIRECT_PEER)


def client_source_trust_mode(settings: Settings) -> str:
    """Non-sensitive deployment verification label for health checks."""
    if settings.admin_trust_proxy_headers:
        return "verified-proxy-hops"
    return "direct-peer-only"
