"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Maximum comma-separated forwarding hops accepted before failing closed.
_MAX_FORWARDING_CHAIN_LENGTH = 10

# Conservative private/link-local ranges for Render's internal proxy boundary.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

# Sample one invalid/untrusted forwarding telemetry event per interval.
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_last_invalid_forwarding_log_at = 0.0

_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"?\[([0-9a-fA-F:.]+)\]"?|"?(?:[0-9a-fA-F:.]+)"?)',
    re.IGNORECASE,
)


class ClientSourceResolutionPath(StrEnum):
    DIRECT_PEER = "direct_peer"
    CF_CONNECTING_IP = "cf_connecting_ip"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED = "forwarded"
    UNKNOWN = "unknown"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: ClientSourceResolutionPath


class _TrustedProxyBoundary:
    """Membership checks for configured trusted proxy CIDRs."""

    def __init__(self, cidrs: tuple[str, ...]) -> None:
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
        self._hosts: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] = ()
        if cidrs:
            networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
            hosts: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
            for cidr in cidrs:
                item = cidr.strip()
                if not item:
                    continue
                if "/" in item:
                    networks.append(ipaddress.ip_network(item, strict=False))
                else:
                    hosts.append(ipaddress.ip_address(item))
            self._networks = tuple(networks)
            self._hosts = tuple(hosts)

    def __bool__(self) -> bool:
        return bool(self._networks or self._hosts)

    @functools.lru_cache(maxsize=4096)
    def contains(self, host: str) -> bool:
        if not host:
            return False
        if len(host) > 253:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip in self._hosts:
            return True
        return any(ip in network for network in self._networks)


def trusted_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    """Return configured trusted proxy CIDRs, with legacy env compatibility."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    if settings.admin_trust_proxy_headers:
        return DEFAULT_RENDER_TRUSTED_PROXY_CIDRS
    return ()


def _parse_host_port(value: str) -> tuple[str, str | None]:
    """Split host and optional port from a forwarding header element."""
    candidate = value.strip()
    if not candidate:
        return "", None

    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            return candidate, None
        host = candidate[1:bracket_end]
        remainder = candidate[bracket_end + 1 :]
        if remainder.startswith(":"):
            return host, remainder[1:]
        return host, None

    if candidate.count(":") > 1:
        return candidate, None

    if candidate.count(":") == 1:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            return host, port
    return candidate, None


def normalize_client_source(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject invalid input."""
    host, _port = _parse_host_port(raw)
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


def _split_forwarding_chain(header_value: str) -> list[str]:
    elements: list[str] = []
    for part in header_value.split(","):
        host, _port = _parse_host_port(part)
        if host:
            elements.append(host)
    return elements


def _collect_x_forwarded_for(request: Request) -> list[str]:
    values: list[str] = []
    for name, value in request.headers.raw:
        if name.decode("latin1").lower() == "x-forwarded-for":
            values.extend(_split_forwarding_chain(value.decode("latin1")))
    return values


def _parse_forwarded_header(request: Request) -> str | None:
    forwarded_values = request.headers.getlist("forwarded")
    if not forwarded_values:
        single = request.headers.get("forwarded")
        if single:
            forwarded_values = [single]
    for header_value in forwarded_values:
        match = _FORWARDED_FOR_RE.search(header_value)
        if match is None:
            continue
        normalized = normalize_client_source(match.group(1))
        if normalized is not None:
            return normalized
    return None


def _resolve_from_forwarding_chain(
    chain_hosts: list[str],
    *,
    boundary: _TrustedProxyBoundary,
) -> str | None:
    if not chain_hosts:
        return None
    if len(chain_hosts) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for host in reversed(chain_hosts):
        normalized = normalize_client_source(host)
        if normalized is None:
            return None
        if not boundary.contains(normalized):
            return normalized
    return None


def _maybe_log_invalid_forwarding(path: ClientSourceResolutionPath) -> None:
    global _last_invalid_forwarding_log_at
    if path is not ClientSourceResolutionPath.INVALID_FORWARDING:
        return
    now = time.monotonic()
    if now - _last_invalid_forwarding_log_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _last_invalid_forwarding_log_at = now
    _logger.warning(
        "Admin login source resolution rejected forwarding headers",
        extra={"source_resolution_path": path.value},
    )


def _fallback_peer_source(peer: str | None) -> str:
    """Return a limiter source for the direct peer when it is not a parseable IP."""
    if not peer or not peer.strip():
        return "unknown"
    normalized = normalize_client_source(peer)
    if normalized is not None:
        return normalized
    return peer.strip().lower()


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host or None


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain: public client → Cloudflare edge → Render load balancer →
    Uvicorn. Forwarding headers are honored only when the immediate peer is a
    member of the configured trusted proxy boundary. Vendor-specific headers
    such as ``CF-Connecting-IP`` are accepted only through that same boundary.
    """
    boundary = _TrustedProxyBoundary(trusted_proxy_cidrs(settings))
    peer = _peer_host(request)
    normalized_peer = normalize_client_source(peer) if peer else None

    if not boundary:
        if peer is None:
            return ClientSourceResolution("unknown", ClientSourceResolutionPath.UNKNOWN)
        return ClientSourceResolution(
            _fallback_peer_source(peer),
            ClientSourceResolutionPath.DIRECT_PEER,
        )

    if peer is None:
        return ClientSourceResolution("unknown", ClientSourceResolutionPath.UNKNOWN)

    if normalized_peer is None:
        return ClientSourceResolution(
            _fallback_peer_source(peer),
            ClientSourceResolutionPath.DIRECT_PEER,
        )

    if not boundary.contains(normalized_peer):
        return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)

    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    if cf_connecting_ip:
        normalized_cf = normalize_client_source(cf_connecting_ip)
        if normalized_cf is not None:
            return ClientSourceResolution(
                normalized_cf,
                ClientSourceResolutionPath.CF_CONNECTING_IP,
            )

    xff_chain = _collect_x_forwarded_for(request)
    if xff_chain:
        chain = [*xff_chain, normalized_peer]
        resolved = _resolve_from_forwarding_chain(chain, boundary=boundary)
        if resolved is not None:
            return ClientSourceResolution(resolved, ClientSourceResolutionPath.X_FORWARDED_FOR)
        result = ClientSourceResolution("unknown", ClientSourceResolutionPath.INVALID_FORWARDING)
        _maybe_log_invalid_forwarding(result.path)
        return result

    forwarded_client = _parse_forwarded_header(request)
    if forwarded_client is not None:
        return ClientSourceResolution(forwarded_client, ClientSourceResolutionPath.FORWARDED)

    return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry state (tests only)."""
    global _last_invalid_forwarding_log_at
    _last_invalid_forwarding_log_at = 0.0
