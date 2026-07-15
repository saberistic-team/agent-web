"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 32
_MAX_INVALID_FORWARD_LOG_INTERVAL_SECONDS = 60.0
_invalid_forward_log_lock = Lock()
_last_invalid_forward_log_at = 0.0

_FORWARDED_FOR_PARAM = re.compile(
    r"""for=(?:"\[([^\]]+)\](?::\d+)?"|([^;,\s"]+))""",
    re.IGNORECASE,
)


class ClientSourceResolutionPath(StrEnum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    X_FORWARDED_FOR_TRUSTED_CHAIN = "x_forwarded_for_trusted_chain"
    INVALID_FORWARDING_FALLBACK_PEER = "invalid_forwarding_fallback_peer"
    UNKNOWN_PEER = "unknown_peer"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting raw forwarding data."""

    source: str
    path: ClientSourceResolutionPath


class TrustedProxyBoundary:
    """Immediate-peer and hop-trust checks for one configured proxy boundary."""

    def __init__(self, trusted_entries: tuple[str, ...]) -> None:
        self.always_trust = trusted_entries == ("*",)
        self.trusted_literals: set[str] = set()
        self.trusted_hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.trusted_networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

        if not self.always_trust:
            for entry in trusted_entries:
                if "/" in entry:
                    try:
                        self.trusted_networks.add(ipaddress.ip_network(entry, strict=False))
                    except ValueError:
                        self.trusted_literals.add(entry)
                else:
                    try:
                        self.trusted_hosts.add(ipaddress.ip_address(entry))
                    except ValueError:
                        self.trusted_literals.add(entry)

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


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Parse comma-separated trusted proxy hosts, literals, or CIDR entries."""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_host_port(value: str) -> tuple[str, int]:
    stripped = value.strip()
    if not stripped:
        return "", 0

    if stripped.startswith("["):
        bracket_end = stripped.find("]")
        if bracket_end == -1:
            return stripped, 0
        host = stripped[1:bracket_end]
        remainder = stripped[bracket_end + 1 :]
        if remainder.startswith(":"):
            try:
                return host, int(remainder[1:])
            except ValueError:
                return host, 0
        return host, 0

    if stripped.count(":") == 1:
        host, port_text = stripped.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return stripped, 0

    return stripped, 0


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    host, _port = _parse_host_port(raw)
    if not host or len(host) > 253:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _split_forwarding_chain(header_value: str) -> list[str]:
    return [segment.strip() for segment in header_value.split(",") if segment.strip()]


def _client_from_trusted_hop_chain(
    hop_values: list[str],
    boundary: TrustedProxyBoundary,
) -> str | None:
    if not hop_values:
        return None
    if len(hop_values) > MAX_FORWARDED_CHAIN_LENGTH:
        return None

    normalized_hops: list[str] = []
    for hop in hop_values:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    for hop in reversed(normalized_hops):
        if hop not in boundary:
            return hop

    return normalized_hops[0]


def _parse_forwarded_header_for_values(header_value: str) -> list[str]:
    values: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_PARAM.search(entry)
        if match is None:
            continue
        captured = match.group(1) or match.group(2) or ""
        if captured:
            values.append(captured.strip())
    return values


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _has_forwarding_headers(request: Request) -> bool:
    header_names = {name.decode("latin1").lower() for name, _value in request.headers.raw}
    return bool(
        header_names.intersection(
            {
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "true-client-ip",
            }
        )
    )


def _log_invalid_forwarding_attempt(path: ClientSourceResolutionPath) -> None:
    global _last_invalid_forward_log_at
    now = time.monotonic()
    with _invalid_forward_log_lock:
        if now - _last_invalid_forward_log_at < _MAX_INVALID_FORWARD_LOG_INTERVAL_SECONDS:
            return
        _last_invalid_forward_log_at = now
    _logger.info(
        "Admin login client source rejected forwarded identity",
        extra={"source_resolution_path": path.value},
    )


def _peer_source_identity(peer_host: str) -> tuple[str, ClientSourceResolutionPath]:
    normalized = normalize_client_address(peer_host)
    if normalized is not None:
        return normalized, ClientSourceResolutionPath.DIRECT_PEER
    stripped = peer_host.strip().lower()
    if stripped:
        return stripped, ClientSourceResolutionPath.DIRECT_PEER
    return "unknown", ClientSourceResolutionPath.UNKNOWN_PEER


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin login source address for rate limiting."""
    boundary = TrustedProxyBoundary(settings.admin_trusted_proxy_cidrs)
    peer_host = _immediate_peer_host(request)

    if peer_host is None:
        return ClientSourceResolution(
            source="unknown",
            path=ClientSourceResolutionPath.UNKNOWN_PEER,
        )

    normalized_peer = normalize_client_address(peer_host)
    peer_source, peer_path = _peer_source_identity(peer_host)

    if normalized_peer is None:
        if _has_forwarding_headers(request):
            _log_invalid_forwarding_attempt(
                ClientSourceResolutionPath.INVALID_FORWARDING_FALLBACK_PEER
            )
        return ClientSourceResolution(source=peer_source, path=peer_path)

    if not boundary.configured() or normalized_peer not in boundary:
        if _has_forwarding_headers(request):
            _log_invalid_forwarding_attempt(ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(source=peer_source, path=peer_path)

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        normalized_cf = normalize_client_address(cf_connecting_ip)
        if normalized_cf is not None:
            return ClientSourceResolution(
                source=normalized_cf,
                path=ClientSourceResolutionPath.CF_CONNECTING_IP,
            )

    forwarded_header = request.headers.get("forwarded", "").strip()
    if forwarded_header:
        forwarded_hops = _parse_forwarded_header_for_values(forwarded_header)
        resolved = _client_from_trusted_hop_chain(forwarded_hops, boundary)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.FORWARDED_HEADER,
            )

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        xff_hops = _split_forwarding_chain(x_forwarded_for)
        resolved = _client_from_trusted_hop_chain(xff_hops, boundary)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.X_FORWARDED_FOR_TRUSTED_CHAIN,
            )

    if _has_forwarding_headers(request):
        _log_invalid_forwarding_attempt(
            ClientSourceResolutionPath.INVALID_FORWARDING_FALLBACK_PEER
        )

    return ClientSourceResolution(
        source=normalized_peer,
        path=ClientSourceResolutionPath.INVALID_FORWARDING_FALLBACK_PEER,
    )


def log_client_source_resolution(resolution: ClientSourceResolution) -> None:
    """Emit bounded telemetry without raw addresses or forwarding headers."""
    _logger.debug(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path.value},
    )
