"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative shared bucket when a trusted proxy omits usable forwarding data.
_TRUSTED_PROXY_FALLBACK_SOURCE = "unknown-trusted-proxy"

# Default Render-internal peers when legacy ADMIN_TRUST_PROXY_HEADERS=true.
_DEFAULT_RENDER_TRUSTED_PROXIES = (
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

# Matches render.yaml / uvicorn --forwarded-allow-ips in production.
PRODUCTION_TRUSTED_PROXY_IPS = ",".join(_DEFAULT_RENDER_TRUSTED_PROXIES)

SourceResolutionPath = Literal[
    "direct_peer",
    "framework_peer",
    "x_forwarded_for",
    "forwarded",
    "cf_connecting_ip",
    "trusted_proxy_fallback",
    "missing_peer",
    "invalid_forwarding",
]

_MAX_FORWARDING_CHAIN_LENGTH = 32
_MAX_FORWARDING_HEADER_LENGTH = 2048
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0

_telemetry_lock = Lock()
_last_invalid_forwarding_log_at = 0.0
_invalid_forwarding_suppressed = 0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source plus a privacy-safe resolution path label."""

    source: str
    path: SourceResolutionPath


class TrustedProxySet:
    """IPv4/IPv6 trusted proxy allowlist with CIDR support."""

    def __init__(self, entries: tuple[str, ...]) -> None:
        self._entries = entries
        self._hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._literals: set[str] = set()
        for entry in entries:
            token = entry.strip()
            if not token:
                continue
            if "/" in token:
                try:
                    self._networks.append(ipaddress.ip_network(token, strict=False))
                except ValueError:
                    self._literals.add(token)
                continue
            try:
                self._hosts.add(ipaddress.ip_address(token))
            except ValueError:
                self._literals.add(token)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __contains__(self, host: str | None) -> bool:
        if not host:
            return False
        if host in self._literals:
            return True
        try:
            ip = ipaddress.ip_address(host.strip())
        except ValueError:
            return False
        if ip in self._hosts:
            return True
        return any(ip in network for network in self._networks)


def trusted_proxy_entries(settings: Settings) -> tuple[str, ...]:
    """Return configured trusted proxy CIDRs/hosts for the active settings."""
    configured = settings.admin_trusted_proxy_ips.strip()
    if configured:
        return tuple(part.strip() for part in configured.split(",") if part.strip())
    if settings.admin_trust_proxy_headers:
        return _DEFAULT_RENDER_TRUSTED_PROXIES
    return ()


def trusted_proxy_set(settings: Settings) -> TrustedProxySet:
    return TrustedProxySet(trusted_proxy_entries(settings))


def normalize_client_source(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 (incl. IPv4-mapped) for deterministic limiter keys."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        # IPv4 with port, e.g. 203.0.113.1:8080
        candidate = candidate.rsplit(":", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv4Address):
        return str(ip)
    return ip.compressed


def _peer_source(peer: str) -> str:
    normalized = normalize_client_source(peer)
    return normalized if normalized is not None else peer.strip().lower() or "unknown"


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() or None


def _split_forwarding_chain(raw: str) -> list[str]:
    if len(raw) > _MAX_FORWARDING_HEADER_LENGTH:
        return []
    parts: list[str] = []
    for segment in raw.split(","):
        token = segment.strip()
        if token:
            parts.append(token)
        if len(parts) > _MAX_FORWARDING_CHAIN_LENGTH:
            return []
    return parts


def _client_from_forwarding_chain(
    chain: list[str],
    trusted: TrustedProxySet,
) -> str | None:
    """Walk X-Forwarded-For right-to-left; first untrusted hop is the client."""
    if not chain:
        return None
    for hop in reversed(chain):
        normalized = normalize_client_source(hop)
        if normalized is None:
            return None
        if normalized not in trusted:
            return normalized
    normalized_first = normalize_client_source(chain[0])
    return normalized_first


def _parse_forwarded_for_header(
    request: Request,
    trusted: TrustedProxySet,
) -> str | None:
    raw = request.headers.get("x-forwarded-for")
    if not raw:
        return None
    chain = _split_forwarding_chain(raw)
    if not chain:
        _record_invalid_forwarding("x_forwarded_for")
        return None
    client = _client_from_forwarding_chain(chain, trusted)
    if client is None:
        _record_invalid_forwarding("x_forwarded_for")
    return client


_FORWARDED_FOR_RE = re.compile(
    r'(?:for=)(?:"\[([^\]]+)\]"|"([^"]+)"|([^;,\s"]+))',
    re.IGNORECASE,
)


def _parse_forwarded_header(
    request: Request,
    trusted: TrustedProxySet,
) -> str | None:
    raw = request.headers.get("forwarded")
    if not raw:
        return None
    if len(raw) > _MAX_FORWARDING_HEADER_LENGTH:
        _record_invalid_forwarding("forwarded")
        return None
    chain: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        host = match.group(1) or match.group(2) or match.group(3)
        if host:
            chain.append(host.strip())
        if len(chain) > _MAX_FORWARDING_CHAIN_LENGTH:
            _record_invalid_forwarding("forwarded")
            return None
    if not chain:
        _record_invalid_forwarding("forwarded")
        return None
    client = _client_from_forwarding_chain(chain, trusted)
    if client is None:
        _record_invalid_forwarding("forwarded")
    return client


def _parse_cf_connecting_ip_header(
    request: Request,
    *,
    trusted: TrustedProxySet,
    xff_client: str | None,
    xff_chain: list[str],
) -> str | None:
    raw = request.headers.get("cf-connecting-ip")
    if not raw:
        return None
    normalized = normalize_client_source(raw)
    if normalized is None:
        _record_invalid_forwarding("cf_connecting_ip")
        return None
    # Require evidence the request transited a multi-hop forwarding chain so a
    # direct Render origin cannot pick a client identity from this header alone.
    if len(xff_chain) < 2 and xff_client is None:
        _record_invalid_forwarding("cf_connecting_ip_untrusted")
        return None
    if xff_client is not None and normalized != xff_client:
        _record_invalid_forwarding("cf_connecting_ip_conflict")
        return None
    if normalized in trusted:
        _record_invalid_forwarding("cf_connecting_ip_trusted_hop")
        return None
    return normalized


def _record_invalid_forwarding(reason: str) -> None:
    """Sample invalid/untrusted forwarding telemetry without recording raw values."""
    global _last_invalid_forwarding_log_at, _invalid_forwarding_suppressed
    now = time.monotonic()
    with _telemetry_lock:
        if now - _last_invalid_forwarding_log_at < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            _invalid_forwarding_suppressed += 1
            return
        suppressed = _invalid_forwarding_suppressed
        _invalid_forwarding_suppressed = 0
        _last_invalid_forwarding_log_at = now
    _logger.info(
        "Admin login client source ignored forwarding data",
        extra={
            "client_source_path": "invalid_forwarding",
            "forwarding_reason": reason,
            "suppressed_since_last": suppressed,
        },
    )


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _last_invalid_forwarding_log_at, _invalid_forwarding_suppressed
    with _telemetry_lock:
        _last_invalid_forwarding_log_at = 0.0
        _invalid_forwarding_suppressed = 0


def _log_resolution(path: SourceResolutionPath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_path": path},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin login limiter source for ``request``.

    Forwarding headers are parsed only when the immediate peer is a configured
    trusted proxy. When Uvicorn has already rewritten ``request.client`` to an
    untrusted client address, that framework-resolved peer wins and raw
    forwarding headers are ignored.
    """
    trusted = trusted_proxy_set(settings)
    peer = _immediate_peer_host(request)

    if peer is None:
        _log_resolution("missing_peer")
        return ClientSourceResolution(source="unknown", path="missing_peer")

    if trusted and peer not in trusted:
        _log_resolution("framework_peer")
        return ClientSourceResolution(source=_peer_source(peer), path="framework_peer")

    if not trusted:
        _log_resolution("direct_peer")
        return ClientSourceResolution(source=_peer_source(peer), path="direct_peer")

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarding_chain(xff_raw) if xff_raw else []
    if xff_raw and not xff_chain:
        _record_invalid_forwarding("x_forwarded_for")
    xff_client = (
        _client_from_forwarding_chain(xff_chain, trusted) if xff_chain else None
    )
    if xff_chain and xff_client is None:
        _record_invalid_forwarding("x_forwarded_for")

    if xff_client is not None:
        _log_resolution("x_forwarded_for")
        return ClientSourceResolution(source=xff_client, path="x_forwarded_for")

    forwarded_client = _parse_forwarded_header(request, trusted)
    if forwarded_client is not None:
        _log_resolution("forwarded")
        return ClientSourceResolution(source=forwarded_client, path="forwarded")

    cf_client = _parse_cf_connecting_ip_header(
        request,
        trusted=trusted,
        xff_client=xff_client,
        xff_chain=xff_chain,
    )
    if cf_client is not None:
        _log_resolution("cf_connecting_ip")
        return ClientSourceResolution(source=cf_client, path="cf_connecting_ip")

    normalized_peer = normalize_client_source(peer)
    if normalized_peer is not None and normalized_peer not in trusted:
        _log_resolution("framework_peer")
        return ClientSourceResolution(source=normalized_peer, path="framework_peer")

    _log_resolution("trusted_proxy_fallback")
    return ClientSourceResolution(
        source=_TRUSTED_PROXY_FALLBACK_SOURCE,
        path="trusted_proxy_fallback",
    )


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for limiter key material."""
    return resolve_admin_login_client_source(request, settings).source
