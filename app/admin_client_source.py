"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from threading import Lock

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 64
MAX_FORWARDING_HEADER_LENGTH = 2048
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0

_telemetry_lock = Lock()
_telemetry_last_logged: dict[str, float] = {}


class ClientSourcePath(StrEnum):
    PROXY_TRUST_DISABLED = "proxy_trust_disabled"
    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    TRUSTED_X_FORWARDED_FOR = "trusted_x_forwarded_for"
    TRUSTED_FORWARDED = "trusted_forwarded"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    MISSING_PEER = "missing_peer"
    MALFORMED = "malformed"
    OVERLONG = "overlong"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without raw forwarding metadata."""

    source: str
    path: ClientSourcePath


class _TrustedProxyBoundary:
    """Trusted proxy hosts and networks for hop verification."""

    def __init__(self, trusted_entries: tuple[str, ...]) -> None:
        self.always_trust = trusted_entries == ("*",)
        self.trusted_literals: set[str] = set()
        self.trusted_hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.trusted_networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

        if not self.always_trust:
            for entry in trusted_entries:
                trimmed = entry.strip()
                if not trimmed:
                    continue
                if "/" in trimmed:
                    try:
                        self.trusted_networks.add(ipaddress.ip_network(trimmed, strict=False))
                    except ValueError:
                        self.trusted_literals.add(trimmed)
                else:
                    try:
                        self.trusted_hosts.add(ipaddress.ip_address(trimmed))
                    except ValueError:
                        self.trusted_literals.add(trimmed)

        self._trusts = lru_cache(maxsize=4096)(self._compute_trust)

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
        except ValueError:
            return host in self.trusted_literals
        return ip in self.trusted_hosts or any(ip in net for net in self.trusted_networks)

    def first_untrusted_hop(self, hops: tuple[str, ...]) -> str | None:
        if not hops:
            return None
        if self.always_trust:
            return hops[0]
        for hop in reversed(hops):
            host, _port = _split_host_port(hop)
            if host not in self:
                return host
        return hops[0]


def _split_host_port(value: str) -> tuple[str, int]:
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

    if value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0

    return value, 0


def normalize_client_ip(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically."""
    candidate = raw.strip().strip('"')
    if not candidate:
        return None

    if candidate.startswith("["):
        host, _port = _split_host_port(candidate)
        candidate = host

    host, _port = _split_host_port(candidate)
    if not host:
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _parse_forwarded_for_hops(header_value: str) -> tuple[str, ...] | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    parts = [part.strip() for part in header_value.split(",")]
    if any(not part for part in parts):
        return None
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return tuple(parts)


_FORWARDED_FOR_RE = re.compile(r"""for=(?:"([^"]+)"|(\[[^\]]+\]|[^;,\s]+))""", re.IGNORECASE)


def _parse_forwarded_hops(header_value: str) -> tuple[str, ...] | None:
    if len(header_value) > MAX_FORWARDING_HEADER_LENGTH:
        return None
    entries = [entry.strip() for entry in header_value.split(",") if entry.strip()]
    if len(entries) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    hops: list[str] = []
    for entry in entries:
        match = _FORWARDED_FOR_RE.search(entry)
        if match is None:
            return None
        hop = match.group(1) or match.group(2) or ""
        hop = hop.strip()
        if not hop or hop.lower() == "unknown":
            return None
        hops.append(hop)
    if not hops:
        return None
    return tuple(hops)


def _boundary_for_settings(settings: Settings) -> _TrustedProxyBoundary:
    return _TrustedProxyBoundary(settings.admin_trusted_proxy_cidrs)


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _resolve_from_hops(
    hops: tuple[str, ...],
    *,
    boundary: _TrustedProxyBoundary,
    path: ClientSourcePath,
) -> ClientSourceResolution | None:
    candidate_host = boundary.first_untrusted_hop(hops)
    if candidate_host is None:
        return None
    normalized = normalize_client_ip(candidate_host)
    if normalized is None:
        return ClientSourceResolution(source="unknown", path=ClientSourcePath.MALFORMED)
    return ClientSourceResolution(source=normalized, path=path)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin login limiter source for one request."""
    peer = _peer_host(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path=ClientSourcePath.MISSING_PEER)

    if not settings.admin_trust_proxy_headers:
        normalized_peer = normalize_client_ip(peer)
        source = normalized_peer if normalized_peer is not None else "unknown"
        return ClientSourceResolution(source=source, path=ClientSourcePath.PROXY_TRUST_DISABLED)

    boundary = _boundary_for_settings(settings)
    if peer not in boundary:
        normalized_peer = normalize_client_ip(peer)
        source = normalized_peer if normalized_peer is not None else "unknown"
        path = ClientSourcePath.DIRECT_PEER
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            hops = _parse_forwarded_for_hops(x_forwarded_for)
            if hops:
                naive_left = normalize_client_ip(hops[0])
                parsed = _resolve_from_hops(
                    hops,
                    boundary=boundary,
                    path=ClientSourcePath.TRUSTED_X_FORWARDED_FOR,
                )
                if (
                    parsed is not None
                    and parsed.source != source
                    and naive_left is not None
                    and naive_left != source
                ):
                    path = ClientSourcePath.UNTRUSTED_PEER
        elif request.headers.get("forwarded") or request.headers.get("cf-connecting-ip"):
            path = ClientSourcePath.UNTRUSTED_PEER
        return ClientSourceResolution(source=source, path=path)

    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for is not None:
        if len(x_forwarded_for) > MAX_FORWARDING_HEADER_LENGTH:
            return ClientSourceResolution(source="unknown", path=ClientSourcePath.OVERLONG)
        hops = _parse_forwarded_for_hops(x_forwarded_for)
        if hops is None:
            return ClientSourceResolution(source="unknown", path=ClientSourcePath.MALFORMED)
        resolution = _resolve_from_hops(
            hops,
            boundary=boundary,
            path=ClientSourcePath.TRUSTED_X_FORWARDED_FOR,
        )
        if resolution is not None:
            return resolution

    forwarded = request.headers.get("forwarded")
    if forwarded is not None:
        hops = _parse_forwarded_hops(forwarded)
        if hops is None:
            return ClientSourceResolution(source="unknown", path=ClientSourcePath.MALFORMED)
        resolution = _resolve_from_hops(
            hops,
            boundary=boundary,
            path=ClientSourcePath.TRUSTED_FORWARDED,
        )
        if resolution is not None:
            return resolution

    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    cf_ray = request.headers.get("cf-ray")
    if cf_connecting_ip is not None and cf_ray:
        if len(cf_connecting_ip) > MAX_FORWARDING_HEADER_LENGTH:
            return ClientSourceResolution(source="unknown", path=ClientSourcePath.OVERLONG)
        normalized = normalize_client_ip(cf_connecting_ip)
        if normalized is None:
            return ClientSourceResolution(source="unknown", path=ClientSourcePath.MALFORMED)
        return ClientSourceResolution(
            source=normalized,
            path=ClientSourcePath.TRUSTED_CF_CONNECTING_IP,
        )

    normalized_peer = normalize_client_ip(peer)
    source = normalized_peer if normalized_peer is not None else "unknown"
    return ClientSourceResolution(source=source, path=ClientSourcePath.DIRECT_PEER)


def log_client_source_telemetry(resolution: ClientSourceResolution) -> None:
    """Emit bounded, privacy-preserving source-resolution telemetry."""
    reason = resolution.path.value
    now = time.monotonic()
    with _telemetry_lock:
        last_logged = _telemetry_last_logged.get(reason)
        if last_logged is not None and now - last_logged < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            return
        _telemetry_last_logged[reason] = now

    log_level = logging.INFO
    if resolution.path in {
        ClientSourcePath.UNTRUSTED_PEER,
        ClientSourcePath.MALFORMED,
        ClientSourcePath.OVERLONG,
        ClientSourcePath.AMBIGUOUS,
    }:
        log_level = logging.WARNING

    _logger.log(
        log_level,
        "Admin login client source resolved",
        extra={"admin_login_source_path": reason},
    )


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry state (tests only)."""
    with _telemetry_lock:
        _telemetry_last_logged.clear()
