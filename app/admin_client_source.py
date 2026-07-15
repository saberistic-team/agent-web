"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import Request

from app.config import Settings

# Conservative bound on comma-separated forwarding chains.
MAX_FORWARDING_CHAIN_LENGTH = 32

# Rate-limit operational telemetry for invalid/untrusted forwarding attempts.
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0

_logger = logging.getLogger(__name__)
_invalid_forwarding_lock = threading.Lock()
_invalid_forwarding_last_logged = 0.0

# Default trusted proxy boundary for Render / private-network hops when legacy
# ``ADMIN_TRUST_PROXY_HEADERS=true`` is set without explicit CIDRs.
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)


class ClientSourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_FORWARDED = "trusted_forwarded"
    MISSING_PEER = "missing_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    INVALID_FORWARDING = "invalid_forwarding"
    OVERLONG_CHAIN = "overlong_chain"
    AMBIGUOUS_FORWARDING = "ambiguous_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source for limiter keying (never logged as raw IP)."""

    source: str
    path: ClientSourceResolutionPath


def default_trusted_proxy_cidrs() -> str:
    return ",".join(_DEFAULT_TRUSTED_PROXY_CIDRS)


def parse_trusted_proxy_networks(
    cidrs: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted proxy CIDRs; ignore empty/invalid entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in cidrs.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses; strip ports and IPv4-mapped IPv6."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host_part, _, port_part = candidate.partition("]")
        host = host_part.lstrip("[")
        if port_part.startswith(":") and port_part[1:].isdigit():
            candidate = host
        else:
            candidate = host
    elif candidate.count(":") == 1 and candidate.rsplit(":", 1)[-1].isdigit():
        host, _port = candidate.rsplit(":", 1)
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


def is_trusted_proxy_address(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def split_forwarding_chain(header_value: str) -> list[str]:
    """Split a comma-separated forwarding header into trimmed hop strings."""
    return [part.strip() for part in header_value.split(",") if part.strip()]


def resolve_client_from_forwarding_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Strip trailing trusted proxy hops; the rightmost remaining hop is the client."""
    if not chain:
        return None
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    normalized_hops: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)

    while normalized_hops and is_trusted_proxy_address(
        normalized_hops[-1],
        trusted_networks,
    ):
        normalized_hops.pop()

    if not normalized_hops:
        return None
    return normalized_hops[-1]


def parse_forwarded_header(for_value: str) -> list[str]:
    """Extract ``for=`` identifiers from an RFC 7239 ``Forwarded`` header."""
    identifiers: list[str] = []
    for entry in for_value.split(","):
        segment = entry.strip()
        if not segment:
            continue
        for directive in segment.split(";"):
            token = directive.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip().strip('"')
            if value.lower() in {"unknown", "_hidden"}:
                continue
            if value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
            identifiers.append(value)
    return identifiers


def effective_trusted_proxy_cidrs(settings: Settings) -> str:
    """Return configured trusted-proxy CIDRs, with legacy env fallback."""
    explicit = settings.admin_trusted_proxy_cidrs.strip()
    if explicit:
        return explicit
    if settings.admin_trust_proxy_headers:
        return default_trusted_proxy_cidrs()
    return ""


def _record_invalid_forwarding(path: ClientSourceResolutionPath) -> None:
    global _invalid_forwarding_last_logged
    now = time.monotonic()
    with _invalid_forwarding_lock:
        if now - _invalid_forwarding_last_logged < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _invalid_forwarding_last_logged = now
    _logger.warning(
        "Admin login client source rejected forwarding headers",
        extra={"client_source_resolution_path": path.value},
    )


def _log_resolution(path: ClientSourceResolutionPath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented): browser → Cloudflare edge → Render load
    balancer → Uvicorn. Forwarding headers are honored only when the immediate
    TCP peer is a member of ``ADMIN_TRUSTED_PROXY_CIDRS``. The left-most raw
    ``X-Forwarded-For`` value is never trusted without walking the chain from
    the trusted edge inward.

    Header precedence (when the immediate peer is trusted):

    1. ``X-Forwarded-For`` — strip trailing trusted hops; the rightmost remaining
       hop is the candidate client.
    2. When ``CF-Connecting-IP`` is present it must match that candidate; this
       blocks direct Render origin access from rotating ``X-Forwarded-For`` alone.
    3. ``Forwarded`` — same parser on extracted ``for=`` values when ``X-Forwarded-For``
       is absent (no ``CF-Connecting-IP`` cross-check; fail closed if absent).
    4. Vendor headers never influence source identity without a validated chain.

    Missing, malformed, overlong, or ambiguous forwarding data falls back to
    ``unknown`` so limiter behavior stays conservative.
    """
    trusted_cidrs = effective_trusted_proxy_cidrs(settings)
    trusted_networks = parse_trusted_proxy_networks(trusted_cidrs)

    if request.client is None:
        resolution = ClientSourceResolution("unknown", ClientSourceResolutionPath.MISSING_PEER)
        _log_resolution(resolution.path)
        return resolution

    peer = normalize_client_address(request.client.host)
    if peer is None:
        resolution = ClientSourceResolution(
            "unknown",
            ClientSourceResolutionPath.INVALID_FORWARDING,
        )
        _record_invalid_forwarding(resolution.path)
        return resolution

    if not trusted_networks or not is_trusted_proxy_address(peer, trusted_networks):
        resolution = ClientSourceResolution(peer, ClientSourceResolutionPath.UNTRUSTED_PEER)
        _log_resolution(resolution.path)
        return resolution

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header.strip():
        chain = split_forwarding_chain(xff_header)
        if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
            resolution = ClientSourceResolution(
                "unknown",
                ClientSourceResolutionPath.OVERLONG_CHAIN,
            )
            _record_invalid_forwarding(resolution.path)
            return resolution
        client = resolve_client_from_forwarding_chain(chain, trusted_networks)
        if client is not None:
            cf_header = request.headers.get("cf-connecting-ip", "").strip()
            if cf_header:
                cf_client = normalize_client_address(cf_header)
                if cf_client is None or cf_client != client:
                    resolution = ClientSourceResolution(
                        "unknown",
                        ClientSourceResolutionPath.INVALID_FORWARDING,
                    )
                    _record_invalid_forwarding(resolution.path)
                    return resolution
            else:
                # Trusted Render hop without Cloudflare edge metadata — fail closed
                # so direct origin access cannot rotate X-Forwarded-For buckets.
                resolution = ClientSourceResolution(
                    "unknown",
                    ClientSourceResolutionPath.AMBIGUOUS_FORWARDING,
                )
                _log_resolution(resolution.path)
                return resolution
            resolution = ClientSourceResolution(client, ClientSourceResolutionPath.TRUSTED_XFF)
            _log_resolution(resolution.path)
            return resolution
        resolution = ClientSourceResolution(
            "unknown",
            ClientSourceResolutionPath.INVALID_FORWARDING,
        )
        _record_invalid_forwarding(resolution.path)
        return resolution

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header.strip():
        chain = parse_forwarded_header(forwarded_header)
        if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
            resolution = ClientSourceResolution(
                "unknown",
                ClientSourceResolutionPath.OVERLONG_CHAIN,
            )
            _record_invalid_forwarding(resolution.path)
            return resolution
        client = resolve_client_from_forwarding_chain(chain, trusted_networks)
        if client is not None:
            resolution = ClientSourceResolution(
                client,
                ClientSourceResolutionPath.TRUSTED_FORWARDED,
            )
            _log_resolution(resolution.path)
            return resolution
        resolution = ClientSourceResolution(
            "unknown",
            ClientSourceResolutionPath.INVALID_FORWARDING,
        )
        _record_invalid_forwarding(resolution.path)
        return resolution

    # Trusted Render hop but no usable forwarding chain — fail closed. Direct
    # origin access cannot spoof Cloudflare-only headers into a distinct bucket.
    if request.headers.get("cf-connecting-ip", "").strip():
        _record_invalid_forwarding(ClientSourceResolutionPath.INVALID_FORWARDING)

    resolution = ClientSourceResolution("unknown", ClientSourceResolutionPath.AMBIGUOUS_FORWARDING)
    _log_resolution(resolution.path)
    return resolution


def client_ip_from_request(request: Request, settings: Settings) -> str:
    """Return the normalized client source string for limiter keying."""
    return resolve_admin_login_client_source(request, settings).source


def resolution_telemetry_fields(resolution: ClientSourceResolution) -> dict[str, Any]:
    """Structured log fields without raw addresses or header values."""
    return {"client_source_resolution_path": resolution.path.value}


def reset_invalid_forwarding_telemetry_for_tests() -> None:
    """Clear rate-limited invalid-forwarding log state (tests only)."""
    global _invalid_forwarding_last_logged
    with _invalid_forwarding_lock:
        _invalid_forwarding_last_logged = 0.0
