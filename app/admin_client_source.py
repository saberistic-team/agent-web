"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_INVALID_FORWARDING_LOG_BURST = 5

# Conservative fallback when peer or forwarding data cannot be resolved.
_UNKNOWN_SOURCE = "unknown"

_invalid_forwarding_log_state = {"window_start": 0.0, "count": 0}


class SourceResolutionPath(str, Enum):
    """Bounded telemetry for how admin login source identity was derived."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_FORWARDED = "trusted_forwarded"
    MISSING_PEER = "missing_peer"
    INVALID_FORWARDING = "invalid_forwarding"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the derivation path used."""

    source: str
    path: SourceResolutionPath


class TrustedProxyBoundary:
    """Parsed allowlist for immediate peers and removable forwarding hops."""

    def __init__(self, spec: str) -> None:
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._hosts: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for entry in _split_proxy_spec(spec):
            if "/" in entry:
                self._networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                self._hosts.append(ipaddress.ip_address(entry))
        self.configured = bool(self._networks or self._hosts)

    def contains(self, address: str) -> bool:
        normalized = normalize_client_address(address)
        if normalized is None:
            return False
        try:
            ip = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        if ip in self._hosts:
            return True
        return any(ip in network for network in self._networks)


def _split_proxy_spec(spec: str) -> list[str]:
    return [part.strip() for part in spec.split(",") if part.strip()]


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically.

    Strips surrounding whitespace, optional quotes, bracketed IPv6, ports, and
    maps IPv4-mapped IPv6 to dotted IPv4.
    """
    candidate = raw.strip().strip('"').strip("'")
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate.partition("]")
        candidate = host[1:]
        if remainder.startswith(":") and remainder[1:].isdigit():
            pass
    elif candidate.count(":") == 1 and candidate.rsplit(":", 1)[-1].isdigit():
        host, _, _port = candidate.rpartition(":")
        if "." in host:
            candidate = host

    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.compressed
    return str(ip)


def parse_x_forwarded_for(header: str) -> list[str] | None:
    """Parse an X-Forwarded-For chain, rejecting malformed or overlong values."""
    if not header or len(header) > 2048:
        return None

    elements = [part.strip() for part in header.split(",")]
    if not elements or len(elements) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    parsed: list[str] = []
    for element in elements:
        if not element:
            return None
        normalized = normalize_client_address(element)
        if normalized is None:
            return None
        parsed.append(normalized)
    return parsed


_FORWARDED_FOR_RE = re.compile(
    r"""for=(?:"(?P<quoted>[^"]+)"|(?P<unquoted>[^;,]+))""",
    re.IGNORECASE,
)


def parse_forwarded_header(header: str) -> list[str] | None:
    """Extract client addresses from an RFC 7239 Forwarded header."""
    if not header or len(header) > 4096:
        return None

    parsed: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header):
        raw_value = match.group("quoted") or match.group("unquoted") or ""
        value = raw_value.strip()
        if value.lower() == "unknown":
            continue
        if value.startswith("_"):
            continue
        normalized = normalize_client_address(value)
        if normalized is None:
            return None
        parsed.append(normalized)
        if len(parsed) > MAX_FORWARDING_CHAIN_LENGTH:
            return None
    return parsed or None


def resolve_client_from_forwarding_chain(
    chain: Iterable[str],
    *,
    immediate_peer: str,
    trusted_boundary: TrustedProxyBoundary,
) -> str | None:
    """Walk a forwarding chain right-to-left across trusted proxy hops."""
    hops = list(chain)
    peer = normalize_client_address(immediate_peer)
    if peer is None:
        return None
    if hops and hops[-1] != peer:
        hops.append(peer)
    elif not hops:
        hops = [peer]

    for hop in reversed(hops):
        if trusted_boundary.contains(hop):
            continue
        return hop

    return hops[0] if hops else None


def _trusted_boundary(settings: Settings) -> TrustedProxyBoundary:
    return TrustedProxyBoundary(settings.admin_trusted_proxy_ips)


def _proxy_trust_enabled(settings: Settings) -> bool:
    return settings.admin_trust_proxy_headers and _trusted_boundary(settings).configured


def _log_invalid_forwarding_attempt() -> None:
    now = time.monotonic()
    state = _invalid_forwarding_log_state
    if now - state["window_start"] >= _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
        state["window_start"] = now
        state["count"] = 0
    state["count"] += 1
    if state["count"] > _INVALID_FORWARDING_LOG_BURST:
        return
    _logger.info(
        "Admin login forwarding headers ignored for untrusted peer",
        extra={"source_resolution_path": SourceResolutionPath.INVALID_FORWARDING.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting."""
    if request.client is None:
        return ClientSourceResolution(_UNKNOWN_SOURCE, SourceResolutionPath.MISSING_PEER)

    peer_raw = request.client.host
    peer = normalize_client_address(peer_raw)
    if peer is None:
        peer = peer_raw.strip() or _UNKNOWN_SOURCE

    if not _proxy_trust_enabled(settings):
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    trusted_boundary = _trusted_boundary(settings)
    if not trusted_boundary.contains(peer):
        if any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        ):
            _log_invalid_forwarding_attempt()
        return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        chain = parse_x_forwarded_for(xff_header)
        if chain is None:
            return ClientSourceResolution(
                peer,
                SourceResolutionPath.INVALID_FORWARDING,
            )
        client = resolve_client_from_forwarding_chain(
            chain,
            immediate_peer=peer,
            trusted_boundary=trusted_boundary,
        )
        if client is not None:
            return ClientSourceResolution(client, SourceResolutionPath.TRUSTED_XFF)
        return ClientSourceResolution(peer, SourceResolutionPath.INVALID_FORWARDING)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        chain = parse_forwarded_header(forwarded_header)
        if chain is None:
            return ClientSourceResolution(
                peer,
                SourceResolutionPath.INVALID_FORWARDING,
            )
        client = resolve_client_from_forwarding_chain(
            chain,
            immediate_peer=peer,
            trusted_boundary=trusted_boundary,
        )
        if client is not None:
            return ClientSourceResolution(client, SourceResolutionPath.TRUSTED_FORWARDED)
        return ClientSourceResolution(peer, SourceResolutionPath.INVALID_FORWARDING)

    # Vendor-specific edge headers are ignored unless a validated forwarding chain
    # is present; direct origin access must not accept spoofed Cloudflare headers.
    return ClientSourceResolution(peer, SourceResolutionPath.DIRECT_PEER)


def log_admin_login_source_resolution(resolution: ClientSourceResolution) -> None:
    """Emit bounded telemetry without raw addresses or forwarding headers."""
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": resolution.path.value},
    )
