"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

# Render private-network boundary shared with Uvicorn ``--forwarded-allow-ips``.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)

RESOLUTION_PATH_DIRECT_PEER = "direct_peer"
RESOLUTION_PATH_CF_CONNECTING_IP = "cf_connecting_ip"
RESOLUTION_PATH_FORWARDED_FOR = "forwarded_for"
RESOLUTION_PATH_FORWARDED_HEADER = "forwarded"
RESOLUTION_PATH_UNKNOWN = "unknown"
RESOLUTION_PATH_UNTRUSTED_FORWARDING = "untrusted_forwarding"

MAX_FORWARDING_CHAIN_LENGTH = 32

_UNTRUSTED_TELEMETRY_LOCK = Lock()
_UNTRUSTED_TELEMETRY_LAST_EMITTED = 0.0
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0

_FORWARDED_PAIR_RE = re.compile(
    r'for=(?:"\[?([^";]+?)\]?"|([^";]+))',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


class _TrustedProxyBoundary:
    """Membership checks for configured trusted proxy hosts and networks."""

    def __init__(self, cidrs: tuple[str, ...]) -> None:
        self._literals: set[str] = set()
        self._hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in cidrs:
            value = entry.strip()
            if not value:
                continue
            if "/" in value:
                try:
                    networks.append(ipaddress.ip_network(value, strict=False))
                except ValueError:
                    self._literals.add(value)
                continue
            try:
                self._hosts.add(ipaddress.ip_address(value))
            except ValueError:
                self._literals.add(value)
        self._networks = tuple(networks)

    @property
    def configured(self) -> bool:
        return bool(self._literals or self._hosts or self._networks)

    def contains(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip in self._hosts:
                return True
            return any(ip in network for network in self._networks)
        except ValueError:
            return host in self._literals


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated trusted-proxy boundary from configuration."""
    if not raw.strip():
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def effective_trusted_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    """Return the configured trusted-proxy boundary for source resolution."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    if settings.admin_trust_proxy_headers:
        return DEFAULT_RENDER_TRUSTED_PROXY_CIDRS
    return ()


def normalize_client_source(value: str) -> str | None:
    """Normalize one client source candidate or return ``None`` when invalid."""
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > 253:
        return None

    host = candidate
    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            return None
        host = candidate[1:bracket_end]
    elif candidate.count(":") == 1 and "." in candidate:
        maybe_host, maybe_port = candidate.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _split_forwarding_chain(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _resolve_from_forwarded_for(
    *,
    forwarded_for: str,
    immediate_peer: str | None,
    boundary: _TrustedProxyBoundary,
) -> str | None:
    chain = _split_forwarding_chain(forwarded_for)
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    if immediate_peer and (not chain or chain[-1] != immediate_peer):
        chain.append(immediate_peer)
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for hop in reversed(chain):
        normalized = normalize_client_source(hop)
        if normalized is None:
            return None
        if not boundary.contains(normalized):
            return normalized
    return None


def _resolve_from_forwarded_header(
    *,
    forwarded: str,
    immediate_peer: str | None,
    boundary: _TrustedProxyBoundary,
) -> str | None:
    candidates: list[str] = []
    for entry in forwarded.split(","):
        match = _FORWARDED_PAIR_RE.search(entry)
        if match is None:
            continue
        candidates.append(match.group(1) or match.group(2) or "")
    if not candidates:
        return None
    if len(candidates) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    if immediate_peer:
        candidates.append(immediate_peer)
    if len(candidates) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    for hop in reversed(candidates):
        normalized = normalize_client_source(hop)
        if normalized is None:
            return None
        if not boundary.contains(normalized):
            return normalized
    return None


def reset_untrusted_forwarding_telemetry() -> None:
    """Clear sampled telemetry state (tests only)."""
    global _UNTRUSTED_TELEMETRY_LAST_EMITTED
    with _UNTRUSTED_TELEMETRY_LOCK:
        _UNTRUSTED_TELEMETRY_LAST_EMITTED = 0.0


def _emit_untrusted_forwarding_telemetry() -> None:
    global _UNTRUSTED_TELEMETRY_LAST_EMITTED
    now = time.monotonic()
    with _UNTRUSTED_TELEMETRY_LOCK:
        if now - _UNTRUSTED_TELEMETRY_LAST_EMITTED < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
            return
        _UNTRUSTED_TELEMETRY_LAST_EMITTED = now
    _logger.info(
        "Admin login source resolution ignored untrusted forwarding headers",
        extra={"source_resolution_path": RESOLUTION_PATH_UNTRUSTED_FORWARDING},
    )


def _has_forwarding_headers(request: Request) -> bool:
    header_names = request.headers.keys()
    return any(
        name in header_names
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def resolve_admin_login_client_source_detail(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source and the resolution path."""
    immediate_peer = request.client.host if request.client is not None else None
    trusted_cidrs = effective_trusted_proxy_cidrs(settings)
    boundary = _TrustedProxyBoundary(trusted_cidrs)
    forwarding_present = _has_forwarding_headers(request)

    if not boundary.configured:
        if forwarding_present:
            _emit_untrusted_forwarding_telemetry()
        if immediate_peer is None:
            return ClientSourceResolution(source="unknown", path=RESOLUTION_PATH_UNKNOWN)
        normalized_peer = normalize_client_source(immediate_peer)
        if normalized_peer is None:
            return ClientSourceResolution(source="unknown", path=RESOLUTION_PATH_UNKNOWN)
        return ClientSourceResolution(source=normalized_peer, path=RESOLUTION_PATH_DIRECT_PEER)

    if immediate_peer is None or not boundary.contains(immediate_peer):
        if forwarding_present:
            _emit_untrusted_forwarding_telemetry()
        if immediate_peer is None:
            return ClientSourceResolution(source="unknown", path=RESOLUTION_PATH_UNKNOWN)
        normalized_peer = normalize_client_source(immediate_peer)
        if normalized_peer is None:
            return ClientSourceResolution(source="unknown", path=RESOLUTION_PATH_UNKNOWN)
        return ClientSourceResolution(source=normalized_peer, path=RESOLUTION_PATH_DIRECT_PEER)

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip and request.headers.get("cf-ray", "").strip():
        normalized_cf = normalize_client_source(cf_connecting_ip)
        if normalized_cf is not None:
            return ClientSourceResolution(
                source=normalized_cf,
                path=RESOLUTION_PATH_CF_CONNECTING_IP,
            )

    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        resolved = _resolve_from_forwarded_for(
            forwarded_for=forwarded_for,
            immediate_peer=immediate_peer,
            boundary=boundary,
        )
        if resolved is not None:
            return ClientSourceResolution(source=resolved, path=RESOLUTION_PATH_FORWARDED_FOR)

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        resolved = _resolve_from_forwarded_header(
            forwarded=forwarded,
            immediate_peer=immediate_peer,
            boundary=boundary,
        )
        if resolved is not None:
            return ClientSourceResolution(source=resolved, path=RESOLUTION_PATH_FORWARDED_HEADER)

    normalized_peer = normalize_client_source(immediate_peer)
    if normalized_peer is not None and not boundary.contains(normalized_peer):
        return ClientSourceResolution(source=normalized_peer, path=RESOLUTION_PATH_DIRECT_PEER)

    return ClientSourceResolution(source="unknown", path=RESOLUTION_PATH_UNKNOWN)


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Return the normalized client source used by admin login rate limiting."""
    return resolve_admin_login_client_source_detail(request, settings).source
