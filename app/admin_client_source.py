"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative cap on forwarded-chain length to bound parsing work.
_MAX_FORWARDED_CHAIN_LENGTH = 64

# Sample at most one untrusted-forwarding telemetry event per interval per process.
_UNTRUSTED_TELEMETRY_INTERVAL_SECONDS = 60.0
_untrusted_telemetry_lock = threading.Lock()
_untrusted_telemetry_last_at = 0.0
_untrusted_telemetry_suppressed = 0


class ClientSourceResolutionPath(StrEnum):
    """Bounded resolution path identifiers (no raw addresses)."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF = "trusted_xff"
    TRUSTED_CF_CONNECTING = "trusted_cf_connecting"
    TRUSTED_FORWARDED = "trusted_forwarded"
    MISSING_PEER = "missing_peer"
    AMBIGUOUS_CHAIN = "ambiguous_chain"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material plus telemetry-safe metadata."""

    source: str
    path: ClientSourceResolutionPath


def _parse_cidr_list(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Union of configured trusted hop and edge proxy CIDRs."""
    return settings.admin_trusted_proxy_cidrs + settings.admin_edge_proxy_cidrs


def _is_trusted_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address in network for network in networks)


def _normalize_host_token(token: str) -> str | None:
    """Normalize one host token to canonical IP text, or None when invalid."""
    cleaned = token.strip().strip('"').strip("'")
    if not cleaned or len(cleaned) > 128:
        return None

    host = cleaned
    if cleaned.startswith("[") and "]" in cleaned:
        host = cleaned[1 : cleaned.index("]")]
    elif cleaned.count(":") == 1 and "." in cleaned:
        host = cleaned.rsplit(":", 1)[0]

    try:
        parsed = ipaddress.ip_address(host.strip())
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _split_forwarded_chain(raw: str) -> list[str]:
    if not raw or len(raw) > 4096:
        return []
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) > _MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return [part for part in parts if part]


def _resolve_from_xff(
    raw_xff: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, ClientSourceResolutionPath]:
    """Walk X-Forwarded-For right-to-left, skipping trusted hops."""
    chain = _split_forwarded_chain(raw_xff)
    if not chain:
        return None, ClientSourceResolutionPath.MALFORMED

    saw_untrusted = False
    for token in reversed(chain):
        normalized = _normalize_host_token(token)
        if normalized is None:
            return None, ClientSourceResolutionPath.MALFORMED
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            return None, ClientSourceResolutionPath.MALFORMED
        if _is_trusted_address(parsed, networks):
            continue
        saw_untrusted = True
        return normalized, ClientSourceResolutionPath.TRUSTED_XFF

    if saw_untrusted:
        return None, ClientSourceResolutionPath.AMBIGUOUS_CHAIN
    return None, ClientSourceResolutionPath.AMBIGUOUS_CHAIN


_FORWARDED_FOR_RE = re.compile(r"for=(\"?\[[^\]]+\]\"?|\"?[^;,\"]+\"?)", re.IGNORECASE)


def _resolve_from_forwarded_header(
    raw_forwarded: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, ClientSourceResolutionPath]:
    if not raw_forwarded or len(raw_forwarded) > 4096:
        return None, ClientSourceResolutionPath.MALFORMED

    candidates: list[str] = []
    for segment in raw_forwarded.split(","):
        match = _FORWARDED_FOR_RE.search(segment)
        if not match:
            continue
        token = match.group(1).strip().strip('"')
        if token.lower() == "unknown":
            continue
        normalized = _normalize_host_token(token)
        if normalized is not None:
            candidates.append(normalized)

    if not candidates:
        return None, ClientSourceResolutionPath.MALFORMED
    if len(candidates) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None, ClientSourceResolutionPath.MALFORMED

    for candidate in reversed(candidates):
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            return None, ClientSourceResolutionPath.MALFORMED
        if _is_trusted_address(parsed, networks):
            continue
        return candidate, ClientSourceResolutionPath.TRUSTED_FORWARDED

    return None, ClientSourceResolutionPath.AMBIGUOUS_CHAIN


def _edge_verified_in_xff(
    raw_xff: str,
    edge_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not edge_networks:
        return False
    for token in _split_forwarded_chain(raw_xff):
        normalized = _normalize_host_token(token)
        if normalized is None:
            continue
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            continue
        if _is_trusted_address(parsed, edge_networks):
            return True
    return False


def _record_untrusted_forwarding(path: ClientSourceResolutionPath) -> None:
    global _untrusted_telemetry_last_at, _untrusted_telemetry_suppressed
    now = time.monotonic()
    with _untrusted_telemetry_lock:
        elapsed = now - _untrusted_telemetry_last_at
        if elapsed < _UNTRUSTED_TELEMETRY_INTERVAL_SECONDS:
            _untrusted_telemetry_suppressed += 1
            return
        suppressed = _untrusted_telemetry_suppressed
        _untrusted_telemetry_suppressed = 0
        _untrusted_telemetry_last_at = now

    extra: dict[str, object] = {"resolution_path": path.value}
    if suppressed:
        extra["suppressed_since_last"] = suppressed
    _logger.info("Admin login client source ignored untrusted forwarding", extra=extra)


def _emit_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    if resolution.path in {
        ClientSourceResolutionPath.MALFORMED,
        ClientSourceResolutionPath.AMBIGUOUS_CHAIN,
    }:
        _record_untrusted_forwarding(resolution.path)
        return

    _logger.debug(
        "Admin login client source resolved",
        extra={"resolution_path": resolution.path.value},
    )


def _direct_peer_source(request: Request) -> ClientSourceResolution:
    if request.client is None:
        return ClientSourceResolution(
            source="unknown",
            path=ClientSourceResolutionPath.MISSING_PEER,
        )
    normalized = _normalize_host_token(request.client.host)
    if normalized is None:
        host = request.client.host.strip().lower()
        if not host:
            return ClientSourceResolution(
                source="unknown",
                path=ClientSourceResolutionPath.MALFORMED,
            )
        return ClientSourceResolution(
            source=host,
            path=ClientSourceResolutionPath.DIRECT_PEER,
        )
    return ClientSourceResolution(
        source=normalized,
        path=ClientSourceResolutionPath.DIRECT_PEER,
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. Candidate addresses are parsed from right to
    left, skipping configured trusted hop and edge proxy networks. Vendor headers
    such as ``CF-Connecting-IP`` are accepted only after an edge hop is verified
    in the forwarding chain.
    """
    trusted_networks = trusted_proxy_networks(settings)
    peer = _direct_peer_source(request)

    if not trusted_networks:
        resolution = peer
        _emit_resolution_telemetry(resolution)
        return resolution

    try:
        peer_ip = ipaddress.ip_address(peer.source)
    except ValueError:
        resolution = peer
        _emit_resolution_telemetry(resolution)
        return resolution

    if not _is_trusted_address(peer_ip, settings.admin_trusted_proxy_cidrs):
        resolution = peer
        _emit_resolution_telemetry(resolution)
        return resolution

    raw_xff = request.headers.get("x-forwarded-for", "")
    raw_forwarded = request.headers.get("forwarded", "")
    raw_cf_connecting = request.headers.get("cf-connecting-ip", "")

    if raw_cf_connecting and _edge_verified_in_xff(raw_xff, settings.admin_edge_proxy_cidrs):
        normalized_cf = _normalize_host_token(raw_cf_connecting)
        if normalized_cf is not None:
            resolution = ClientSourceResolution(
                source=normalized_cf,
                path=ClientSourceResolutionPath.TRUSTED_CF_CONNECTING,
            )
            _emit_resolution_telemetry(resolution)
            return resolution

    xff_path: ClientSourceResolutionPath | None = None
    if raw_xff:
        resolved, xff_path = _resolve_from_xff(raw_xff, trusted_networks)
        if resolved is not None:
            resolution = ClientSourceResolution(source=resolved, path=xff_path)
            _emit_resolution_telemetry(resolution)
            return resolution
        if xff_path is ClientSourceResolutionPath.AMBIGUOUS_CHAIN:
            resolution = ClientSourceResolution(source="unknown", path=xff_path)
            _emit_resolution_telemetry(resolution)
            return resolution

    if raw_forwarded:
        resolved, forwarded_path = _resolve_from_forwarded_header(raw_forwarded, trusted_networks)
        if resolved is not None:
            resolution = ClientSourceResolution(source=resolved, path=forwarded_path)
            _emit_resolution_telemetry(resolution)
            return resolution
        if forwarded_path in {
            ClientSourceResolutionPath.MALFORMED,
            ClientSourceResolutionPath.AMBIGUOUS_CHAIN,
        }:
            resolution = ClientSourceResolution(source="unknown", path=forwarded_path)
            _emit_resolution_telemetry(resolution)
            return resolution

    if xff_path is ClientSourceResolutionPath.MALFORMED:
        resolution = ClientSourceResolution(source="unknown", path=xff_path)
        _emit_resolution_telemetry(resolution)
        return resolution

    resolution = peer
    _emit_resolution_telemetry(resolution)
    return resolution


def resolve_admin_login_client_source_text(request: Request, settings: Settings) -> str:
    """Return normalized source material for limiter key derivation."""
    return resolve_admin_login_client_source(request, settings).source


def reset_client_source_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _untrusted_telemetry_last_at, _untrusted_telemetry_suppressed
    with _untrusted_telemetry_lock:
        _untrusted_telemetry_last_at = 0.0
        _untrusted_telemetry_suppressed = 0
