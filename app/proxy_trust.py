"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from uvicorn.middleware.proxy_headers import (
    _TrustedHosts,
    _parse_host_port,
    _parse_raw_hosts,
)

_logger = logging.getLogger(__name__)

# Conservative upper bound for comma-separated forwarding chains.
MAX_FORWARDED_CHAIN_LENGTH = 32

# Default Render-internal / private networks used when proxy trust is enabled
# without an explicit CIDR list (see docs/ADMIN_AUTH.md).
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

# Production uvicorn forwarded-allow-ips (private RFC1918 only; no wildcard).
PRODUCTION_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

_FORWARDED_FOR_PARAM = re.compile(
    r'for=(?:"([^"]+)"|\[([^\]]+)\]|([^;,\s"]+))',
    re.IGNORECASE,
)

_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0
_invalid_telemetry_last_logged = 0.0
_invalid_telemetry_suppressed = 0


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED = "forwarded"
    CF_CONNECTING_IP = "cf_connecting_ip"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and observability metadata."""

    source: str
    path: SourceResolutionPath


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated trusted-proxy CIDR list."""
    if not raw.strip():
        return DEFAULT_TRUSTED_PROXY_CIDRS
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def normalize_client_address(raw: str | None) -> str | None:
    """Return a deterministic client address string or ``None`` when invalid."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > 253:
        return None

    host, _port = _parse_host_port(candidate)
    host = host.strip()
    if not host:
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _trusted_hosts(trusted_proxy_cidrs: tuple[str, ...]) -> _TrustedHosts:
    return _TrustedHosts(",".join(trusted_proxy_cidrs))


def _peer_is_trusted(socket_peer: str | None, trusted_hosts: _TrustedHosts) -> bool:
    if not socket_peer:
        return False
    return socket_peer in trusted_hosts


def _walk_forwarded_chain(
    chain: str,
    *,
    trusted_hosts: _TrustedHosts,
) -> str | None:
    hops = _parse_raw_hosts(chain)
    if not hops or len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None

    for hop in reversed(hops):
        host, _port = _parse_host_port(hop)
        if not host:
            return None
        if host not in trusted_hosts:
            return normalize_client_address(host)

    return None


def _parse_forwarded_header(value: str, *, trusted_hosts: _TrustedHosts) -> str | None:
    if not value.strip():
        return None

    candidates: list[str] = []
    for part in value.split(","):
        match = _FORWARDED_FOR_PARAM.search(part)
        if match is None:
            continue
        candidates.append(match.group(1) or match.group(2) or match.group(3))

    if not candidates or len(candidates) > MAX_FORWARDED_CHAIN_LENGTH:
        return None

    for candidate in reversed(candidates):
        host, _port = _parse_host_port(candidate.strip())
        if not host:
            return None
        if host not in trusted_hosts:
            return normalize_client_address(host)
    return None


def _record_invalid_forwarding_attempt(reason: str) -> None:
    global _invalid_telemetry_last_logged, _invalid_telemetry_suppressed

    now = time.monotonic()
    if now - _invalid_telemetry_last_logged < _INVALID_TELEMETRY_INTERVAL_SECONDS:
        _invalid_telemetry_suppressed += 1
        return

    extra: dict[str, object] = {
        "reason": reason,
        "source_resolution_path": SourceResolutionPath.INVALID.value,
    }
    if _invalid_telemetry_suppressed:
        extra["suppressed_since_last_sample"] = _invalid_telemetry_suppressed
        _invalid_telemetry_suppressed = 0
    _invalid_telemetry_last_logged = now
    _logger.warning("Admin login forwarding headers rejected", extra=extra)


def reset_invalid_forwarding_telemetry() -> None:
    """Clear sampled invalid-forwarding counters (tests only)."""
    global _invalid_telemetry_last_logged, _invalid_telemetry_suppressed
    _invalid_telemetry_last_logged = 0.0
    _invalid_telemetry_suppressed = 0


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def resolve_admin_login_client_source(
    *,
    socket_peer: str | None,
    headers: Mapping[str, str],
    trust_proxy_headers: bool,
    trusted_proxy_cidrs: tuple[str, ...],
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Trust model (production: Client → Cloudflare → Render load balancer → Uvicorn):

    1. When proxy trust is disabled, only the socket peer is used; forwarding
       headers are ignored so direct clients cannot spoof source identity.
    2. When proxy trust is enabled, forwarding headers are honored only if the
       immediate socket peer is a member of ``trusted_proxy_cidrs``.
    3. For trusted peers, header precedence is documented in ``docs/ADMIN_AUTH.md``:
       ``CF-Connecting-IP`` (Cloudflare-overwritten at the edge), then
       ``X-Forwarded-For`` right-to-left trusted-hop walk, then RFC 7239
       ``Forwarded``. Vendor headers sent directly to the public Render origin
       are ignored because the socket peer is not trusted.
    """
    peer_source = normalize_client_address(socket_peer)
    if not trust_proxy_headers:
        if peer_source is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.MISSING)
        return ClientSourceResolution(peer_source, SourceResolutionPath.DIRECT_PEER)

    trusted_hosts = _trusted_hosts(trusted_proxy_cidrs)
    if not _peer_is_trusted(socket_peer, trusted_hosts):
        if any(
            _header_value(headers, name)
            for name in (
                "x-forwarded-for",
                "forwarded",
                "cf-connecting-ip",
                "x-real-ip",
            )
        ):
            _record_invalid_forwarding_attempt("untrusted_immediate_peer")
        if peer_source is None:
            return ClientSourceResolution("unknown", SourceResolutionPath.UNTRUSTED_PEER)
        return ClientSourceResolution(peer_source, SourceResolutionPath.UNTRUSTED_PEER)

    cf_connecting_ip = normalize_client_address(_header_value(headers, "cf-connecting-ip"))
    x_forwarded_for = (_header_value(headers, "x-forwarded-for") or "").strip()
    forwarded = (_header_value(headers, "forwarded") or "").strip()

    xff_client = (
        _walk_forwarded_chain(x_forwarded_for, trusted_hosts=trusted_hosts)
        if x_forwarded_for
        else None
    )
    forwarded_client = (
        _parse_forwarded_header(forwarded, trusted_hosts=trusted_hosts)
        if forwarded
        else None
    )

    if cf_connecting_ip is not None:
        if xff_client is not None and xff_client != cf_connecting_ip:
            _record_invalid_forwarding_attempt("header_family_conflict")
            return ClientSourceResolution(xff_client, SourceResolutionPath.X_FORWARDED_FOR)
        if forwarded_client is not None and forwarded_client != cf_connecting_ip:
            _record_invalid_forwarding_attempt("header_family_conflict")
            return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED)
        return ClientSourceResolution(cf_connecting_ip, SourceResolutionPath.CF_CONNECTING_IP)

    if xff_client is not None:
        if forwarded_client is not None and forwarded_client != xff_client:
            _record_invalid_forwarding_attempt("header_family_conflict")
        return ClientSourceResolution(xff_client, SourceResolutionPath.X_FORWARDED_FOR)

    if forwarded_client is not None:
        return ClientSourceResolution(forwarded_client, SourceResolutionPath.FORWARDED)

    if x_forwarded_for or forwarded or _header_value(headers, "cf-connecting-ip"):
        _record_invalid_forwarding_attempt("malformed_forwarding_chain")

    if peer_source is None:
        return ClientSourceResolution("unknown", SourceResolutionPath.INVALID)
    return ClientSourceResolution(peer_source, SourceResolutionPath.INVALID)


def log_source_resolution(path: SourceResolutionPath) -> None:
    """Emit bounded structured telemetry without raw addresses or header values."""
    _logger.info(
        "Admin login source resolved",
        extra={"source_resolution_path": path.value},
    )
