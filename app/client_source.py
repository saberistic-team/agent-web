"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Render web services receive traffic from the platform load balancer on private
# addresses. Uvicorn ``--forwarded-allow-ips`` and ``ADMIN_TRUSTED_PROXY_IPS``
# must stay aligned (see ``render.yaml`` and ``docs/ADMIN_AUTH.md``).
RENDER_FORWARDED_ALLOW_IPS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1"
)

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0
_untrusted_telemetry_lock = threading.Lock()
_last_untrusted_telemetry_at = 0.0


class ClientSourcePath(str, Enum):
    """Resolution path identifiers for operational telemetry (no raw IPs)."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_CHAIN = "trusted_chain"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    TRUSTED_FORWARDED = "trusted_forwarded"
    UNTRUSTED_FORWARDING_IGNORED = "untrusted_forwarding_ignored"
    MALFORMED_FORWARDING = "malformed_forwarding"
    MISSING_PEER = "missing_peer"
    ALL_TRUSTED_CHAIN = "all_trusted_chain"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Normalized client source plus a bounded telemetry label."""

    address: str
    path: ClientSourcePath


@dataclass(frozen=True)
class _TrustedProxyBoundary:
    hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]
    literals: frozenset[str]

    @classmethod
    def from_entries(cls, entries: Iterable[str]) -> _TrustedProxyBoundary:
        hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
        literals: set[str] = set()
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry:
                continue
            if "/" in entry:
                try:
                    networks.add(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    literals.add(entry)
                continue
            try:
                hosts.add(ipaddress.ip_address(entry))
            except ValueError:
                literals.add(entry)
        return cls(
            hosts=frozenset(hosts),
            networks=frozenset(networks),
            literals=frozenset(literals),
        )

    def configured(self) -> bool:
        return bool(self.hosts or self.networks or self.literals)

    def trusts(self, host: str) -> bool:
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host in self.literals
        if ip in self.hosts:
            return True
        return any(ip in network for network in self.networks)


def parse_trusted_proxy_entries(raw: str) -> tuple[str, ...]:
    """Parse comma-separated trusted proxy IPs or CIDRs."""
    return tuple(
        entry.strip()
        for entry in raw.split(",")
        if entry.strip()
    )


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 deterministically; return ``None`` when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def _split_forwarding_chain(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _first_untrusted_from_right(
    chain: list[str],
    boundary: _TrustedProxyBoundary,
) -> str | None:
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    for host_port in reversed(chain):
        host, _port = _parse_host_port(host_port)
        trust_host = _host_for_trust_check(host)
        if not trust_host:
            return None
        if not boundary.trusts(trust_host):
            normalized = normalize_client_address(host)
            return normalized if normalized is not None else trust_host
    if not chain:
        return None
    normalized = normalize_client_address(_parse_host_port(chain[0])[0])
    return normalized


def _parse_host_port(value: str) -> tuple[str, int]:
    if value.startswith("["):
        bracket_end = value.find("]")
        if bracket_end == -1:
            return value, 0
        host = value[1:bracket_end]
        remainder = value[bracket_end + 1 :]
        if remainder.startswith(":"):
            port_text = remainder[1:]
            if port_text.isdigit():
                return host, int(port_text)
        return host, 0
    if value.count(":") == 1 and "." in value:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text)
    return value, 0


def _parse_forwarded_for_values(values: Iterable[str]) -> list[str]:
    chain: list[str] = []
    for value in values:
        chain.extend(_split_forwarding_chain(value))
    return chain


def _parse_forwarded_header(raw: str) -> list[str]:
    """Extract ``for=`` identifiers from an RFC 7239 ``Forwarded`` header."""
    identifiers: list[str] = []
    for element in raw.split(","):
        for directive in element.split(";"):
            name, _, value = directive.partition("=")
            if name.strip().lower() != "for":
                continue
            candidate = value.strip().strip('"')
            if candidate.lower() == "unknown":
                continue
            if candidate.startswith("["):
                end = candidate.find("]")
                if end != -1:
                    candidate = candidate[1:end]
            identifiers.append(candidate)
    return identifiers


def _peer_address(peer: str) -> str:
    normalized = normalize_client_address(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip()
    return stripped or "unknown"


def _host_for_trust_check(host: str) -> str:
    return normalize_client_address(host) or host.strip()


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    return host or None


def _emit_resolution_telemetry(path: ClientSourcePath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"admin_client_source_path": path.value},
    )


def _emit_untrusted_forwarding_telemetry() -> None:
    global _last_untrusted_telemetry_at
    now = time.monotonic()
    with _untrusted_telemetry_lock:
        if now - _last_untrusted_telemetry_at < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
            return
        _last_untrusted_telemetry_at = now
    _logger.info(
        "Ignored untrusted admin login forwarding headers",
        extra={"admin_client_source_path": ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_IPS``. The left-most raw ``X-Forwarded-For`` value is
    never trusted directly; the chain is parsed right-to-left, skipping trusted
    proxy hops (matching Uvicorn's ``ProxyHeadersMiddleware`` semantics).
    """
    boundary = _TrustedProxyBoundary.from_entries(settings.admin_trusted_proxy_ips)
    peer = _peer_host(request)
    if peer is None:
        resolution = ClientSourceResolution("unknown", ClientSourcePath.MISSING_PEER)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    normalized_peer = _peer_address(peer)
    if normalized_peer == "unknown" and not peer.strip():
        resolution = ClientSourceResolution("unknown", ClientSourcePath.MALFORMED_FORWARDING)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    if not boundary.configured():
        if settings.admin_trust_proxy_headers:
            _logger.warning(
                "ADMIN_TRUST_PROXY_HEADERS is set without ADMIN_TRUSTED_PROXY_IPS; "
                "ignoring forwarding headers for admin login source resolution"
            )
        if _has_forwarding_headers(request):
            _emit_untrusted_forwarding_telemetry()
            path = ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED
        else:
            path = ClientSourcePath.DIRECT_PEER
        resolution = ClientSourceResolution(normalized_peer, path)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    if not boundary.trusts(normalized_peer):
        if _has_forwarding_headers(request):
            _emit_untrusted_forwarding_telemetry()
        resolution = ClientSourceResolution(
            normalized_peer,
            ClientSourcePath.UNTRUSTED_FORWARDING_IGNORED,
        )
        _emit_resolution_telemetry(resolution.path)
        return resolution

    xff_values = [
        value.decode("latin1")
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"x-forwarded-for"
    ]
    xff_chain = _parse_forwarded_for_values(xff_values)
    if xff_chain:
        resolved = _first_untrusted_from_right(xff_chain, boundary)
        if resolved is None:
            resolution = ClientSourceResolution(
                normalized_peer,
                ClientSourcePath.MALFORMED_FORWARDING,
            )
            _emit_resolution_telemetry(resolution.path)
            return resolution
        path = (
            ClientSourcePath.ALL_TRUSTED_CHAIN
            if all(
                boundary.trusts(_host_for_trust_check(_parse_host_port(part)[0]))
                for part in xff_chain
            )
            else ClientSourcePath.TRUSTED_CHAIN
        )
        resolution = ClientSourceResolution(resolved, path)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    forwarded_raw = request.headers.get("forwarded", "")
    if forwarded_raw:
        forwarded_chain = _parse_forwarded_header(forwarded_raw)
        resolved = _first_untrusted_from_right(forwarded_chain, boundary)
        if resolved is None:
            resolution = ClientSourceResolution(
                normalized_peer,
                ClientSourcePath.MALFORMED_FORWARDING,
            )
            _emit_resolution_telemetry(resolution.path)
            return resolution
        resolution = ClientSourceResolution(resolved, ClientSourcePath.TRUSTED_FORWARDED)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip:
        resolved = normalize_client_address(cf_connecting_ip)
        if resolved is None:
            resolution = ClientSourceResolution(
                normalized_peer,
                ClientSourcePath.MALFORMED_FORWARDING,
            )
            _emit_resolution_telemetry(resolution.path)
            return resolution
        resolution = ClientSourceResolution(resolved, ClientSourcePath.TRUSTED_CF_CONNECTING_IP)
        _emit_resolution_telemetry(resolution.path)
        return resolution

    resolution = ClientSourceResolution(normalized_peer, ClientSourcePath.DIRECT_PEER)
    _emit_resolution_telemetry(resolution.path)
    return resolution


def _has_forwarding_headers(request: Request) -> bool:
    lowered = {name.decode("latin1").lower() for name, _value in request.scope.get("headers", [])}
    return bool(
        lowered.intersection(
            {
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "x-real-ip",
            }
        )
    )


def admin_proxy_trust_configured(settings: Settings) -> bool:
    """Return whether explicit trusted-proxy boundaries are configured."""
    return _TrustedProxyBoundary.from_entries(settings.admin_trusted_proxy_ips).configured()
