"""Trusted-proxy client source resolution for admin login rate limiting.

Production chain (saberistic.com):

    Browser → Cloudflare edge → Render load balancer → Uvicorn (ProxyHeadersMiddleware)

``FORWARDED_ALLOW_IPS`` is the single source of truth for both Uvicorn's
``--forwarded-allow-ips`` boundary and application-side hop verification.
When the immediate peer is not in that boundary, all forwarding and vendor
headers are ignored and the direct peer address is used.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative cap on comma-separated forwarding hops.
_MAX_FORWARDING_CHAIN_LENGTH = 50

# Sampled telemetry for rejected spoofing attempts (no raw addresses in logs).
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0
_untrusted_telemetry_last_logged = 0.0
_untrusted_telemetry_count = 0


class ClientSourceResolutionPath(str, Enum):
    """Which path produced the limiter client source (no raw IP values)."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    TRUSTED_FORWARDED_HEADER = "trusted_forwarded_header"
    UNKNOWN_PEER = "unknown_peer"
    UNTRUSTED_FORWARDING_REJECTED = "untrusted_forwarding_rejected"
    MALFORMED_FORWARDING = "malformed_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source plus observability metadata."""

    source: str
    path: ClientSourceResolutionPath


def _parse_raw_hosts(value: str) -> list[str]:
    return [item.strip() for item in value.split(",")]


def parse_host_port(value: str) -> tuple[str, int]:
    """Parse a host literal, optionally with port (IPv4 or bracketed IPv6)."""
    if value.startswith("["):
        bracket_end = value.find("]")
        if bracket_end == -1:
            return value, 0

        host = value[1:bracket_end]
        remainder = value[bracket_end + 1 :]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return value, 0

        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0

    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0

    return value, 0


class TrustedProxyBoundary:
    """Trusted proxy IPs and CIDRs (mirrors Uvicorn forwarded-allow-ips semantics)."""

    def __init__(self, trusted_hosts: Iterable[str] | str) -> None:
        hosts = trusted_hosts
        if isinstance(hosts, str):
            hosts = _parse_raw_hosts(hosts) if hosts else []

        self.always_trust = hosts in ("*", ["*"])
        self.trusted_literals: set[str] = set()
        self.trusted_hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.trusted_networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

        if not self.always_trust:
            for host in hosts:
                host = host.strip()
                if not host:
                    continue
                if "/" in host:
                    try:
                        self.trusted_networks.add(ipaddress.ip_network(host, strict=False))
                    except ValueError:
                        self.trusted_literals.add(host)
                else:
                    try:
                        self.trusted_hosts.add(ipaddress.ip_address(host))
                    except ValueError:
                        self.trusted_literals.add(host)

        self._trusts = functools.lru_cache(maxsize=4096)(self._compute_trust)

    def __contains__(self, host: str | None) -> bool:
        if self.always_trust:
            return True
        if not host:
            return False
        if len(host) > 253:
            return self._compute_trust(host)
        return self._trusts(host)

    def _compute_trust(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
            return ip in self.trusted_hosts or any(ip in net for net in self.trusted_networks)
        except ValueError:
            return host in self.trusted_literals

    def configured(self) -> bool:
        return self.always_trust or bool(
            self.trusted_literals or self.trusted_hosts or self.trusted_networks
        )

    def client_from_x_forwarded_for(self, x_forwarded_for: str) -> tuple[str, int] | None:
        """Return the first untrusted hop walking XFF right-to-left (Uvicorn semantics)."""
        hosts = [host for host in _parse_raw_hosts(x_forwarded_for) if host]
        if not hosts or len(hosts) > _MAX_FORWARDING_CHAIN_LENGTH:
            return None

        if self.always_trust:
            return parse_host_port(hosts[0])

        for host_port in reversed(hosts):
            host, port = parse_host_port(host_port)
            if not host:
                continue
            if host not in self:
                return host, port

        return parse_host_port(hosts[0])


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 deterministically; map IPv4-mapped IPv6 to IPv4."""
    host, _ = parse_host_port(raw.strip())
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _boundary_from_settings(settings: Settings) -> TrustedProxyBoundary:
    return TrustedProxyBoundary(settings.forwarded_allow_ips)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _chain_contains_trusted_cloudflare_hop(
    x_forwarded_for: str,
    boundary: TrustedProxyBoundary,
    *,
    cloudflare_cidrs: str,
) -> bool:
    if not cloudflare_cidrs.strip():
        return False
    cf_boundary = TrustedProxyBoundary(cloudflare_cidrs)
    for host_port in _parse_raw_hosts(x_forwarded_for):
        host, _ = parse_host_port(host_port)
        if host and host in cf_boundary:
            return True
    return False


def _parse_forwarded_for_header(header_value: str) -> list[str]:
    """Extract ``for=`` client tokens from RFC 7239 Forwarded header values."""
    clients: list[str] = []
    for element in header_value.split(","):
        for part in element.split(";"):
            part = part.strip()
            if not part.lower().startswith("for="):
                continue
            token = part[4:].strip().strip('"')
            if token.startswith("[") and "]" in token:
                token = token[1 : token.index("]")]
            elif token.count(":") > 1:
                token = token.split("]")[0].lstrip("[")
            clients.append(token)
    return clients


def _client_from_forwarded_header(
    header_value: str,
    boundary: TrustedProxyBoundary,
) -> str | None:
    clients = _parse_forwarded_for_header(header_value)
    if not clients or len(clients) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None

    if boundary.always_trust:
        return normalize_client_address(clients[0])

    for candidate in reversed(clients):
        host, _ = parse_host_port(candidate)
        if host not in boundary:
            return normalize_client_address(host)
    return normalize_client_address(clients[0])


def _record_untrusted_forwarding_attempt() -> None:
    global _untrusted_telemetry_last_logged, _untrusted_telemetry_count
    _untrusted_telemetry_count += 1
    now = time.monotonic()
    if now - _untrusted_telemetry_last_logged < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
        return
    _logger.info(
        "Admin login client source rejected untrusted forwarding headers",
        extra={
            "resolution_path": ClientSourceResolutionPath.UNTRUSTED_FORWARDING_REJECTED.value,
            "sampled_rejections": _untrusted_telemetry_count,
        },
    )
    _untrusted_telemetry_last_logged = now
    _untrusted_telemetry_count = 0


def reset_proxy_trust_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _untrusted_telemetry_last_logged, _untrusted_telemetry_count
    _untrusted_telemetry_last_logged = 0.0
    _untrusted_telemetry_count = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    boundary = _boundary_from_settings(settings)
    peer = _immediate_peer_host(request)
    normalized_peer = normalize_client_address(peer) if peer else None

    if not peer:
        return ClientSourceResolution(
            source="unknown",
            path=ClientSourceResolutionPath.UNKNOWN_PEER,
        )

    if not boundary.configured() or peer not in boundary:
        if _has_forwarding_headers(request):
            _record_untrusted_forwarding_attempt()
        source = normalized_peer or "unknown"
        path = (
            ClientSourceResolutionPath.UNKNOWN_PEER
            if source == "unknown"
            else ClientSourceResolutionPath.DIRECT_PEER
        )
        return ClientSourceResolution(source=source, path=path)

    xff = request.headers.get("x-forwarded-for", "").strip()
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    forwarded = request.headers.get("forwarded", "").strip()

    if cf_connecting_ip and xff:
        if _chain_contains_trusted_cloudflare_hop(
            xff,
            boundary,
            cloudflare_cidrs=settings.cloudflare_trusted_cidrs,
        ):
            normalized_cf = normalize_client_address(cf_connecting_ip)
            if normalized_cf:
                return ClientSourceResolution(
                    source=normalized_cf,
                    path=ClientSourceResolutionPath.CF_CONNECTING_IP,
                )

    if xff:
        resolved = boundary.client_from_x_forwarded_for(xff)
        if resolved is not None:
            host, _ = resolved
            normalized = normalize_client_address(host)
            if normalized:
                return ClientSourceResolution(
                    source=normalized,
                    path=ClientSourceResolutionPath.TRUSTED_XFF_CHAIN,
                )
        return ClientSourceResolution(
            source=normalized_peer or "unknown",
            path=ClientSourceResolutionPath.MALFORMED_FORWARDING,
        )

    if forwarded:
        normalized = _client_from_forwarded_header(forwarded, boundary)
        if normalized:
            return ClientSourceResolution(
                source=normalized,
                path=ClientSourceResolutionPath.TRUSTED_FORWARDED_HEADER,
            )
        return ClientSourceResolution(
            source=normalized_peer or "unknown",
            path=ClientSourceResolutionPath.MALFORMED_FORWARDING,
        )

    source = normalized_peer or "unknown"
    path = (
        ClientSourceResolutionPath.UNKNOWN_PEER
        if source == "unknown"
        else ClientSourceResolutionPath.DIRECT_PEER
    )
    return ClientSourceResolution(source=source, path=path)


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def proxy_trust_configured(settings: Settings) -> bool:
    """Return whether explicit trusted-proxy settings are active."""
    return _boundary_from_settings(settings).configured()
