"""Verified client-source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from fastapi import Request

from app.config import Settings

MAX_FORWARDED_CHAIN_LENGTH = 32
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_UNTRUSTED_FORWARDING_LOG_LIMIT = 10

_logger = logging.getLogger(__name__)
_untrusted_forwarding_log_state: dict[str, tuple[int, float]] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a telemetry-safe path label."""

    source: str
    path: str


def normalize_client_source(value: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv6Address):
        return addr.compressed
    return str(addr)


def _source_from_peer(peer: str | None) -> str:
    """Return a limiter-safe source identity derived from the direct peer."""
    if peer is None:
        return "unknown"
    normalized = normalize_client_source(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip()
    return stripped.lower() if stripped else "unknown"


def immediate_peer_host(request: Request) -> str | None:
    """Return the raw TCP peer captured before proxy-header rewriting."""
    peer = request.scope.get("immediate_peer")
    if isinstance(peer, str) and peer.strip():
        return peer.strip()
    if request.client is not None and request.client.host:
        return request.client.host.strip()
    return None


def _ip_in_trusted_networks(
    host: str,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_client_source(host)
    if normalized is None:
        return False
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(addr in network for network in trusted_networks)


def _split_forwarded_for(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_forwarded_header(value: str) -> list[str]:
    """Extract ``for=`` identifiers from RFC 7239 Forwarded values."""
    hosts: list[str] = []
    for entry in value.split(","):
        piece = entry.strip()
        if not piece:
            continue
        match = re.search(r"for=(\"([^\"]+)\"|([^;,\s]+))", piece, flags=re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(2) or match.group(3) or ""
        raw = raw.strip()
        if raw.lower() == "unknown":
            continue
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        hosts.append(raw)
    return hosts


def _resolve_from_hops(
    hops: Iterable[str],
    *,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> tuple[str | None, str]:
    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_client_source(hop)
        if normalized is None:
            return None, "invalid_forwarding_data"
        normalized_hops.append(normalized)

    if len(normalized_hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None, "invalid_forwarding_data"

    if not normalized_hops:
        return None, "invalid_forwarding_data"

    for hop in reversed(normalized_hops):
        if not _ip_in_trusted_networks(hop, trusted_networks):
            return hop, "verified_forwarded_chain"

    return normalized_hops[0], "verified_forwarded_chain"


def _forwarding_header_families(request: Request) -> tuple[str, ...]:
    families: list[str] = []
    if request.headers.get("x-forwarded-for"):
        families.append("x_forwarded_for")
    if request.headers.get("forwarded"):
        families.append("forwarded")
    if request.headers.get("cf-connecting-ip"):
        families.append("cf_connecting_ip")
    return tuple(families)


def _record_untrusted_forwarding_attempt(header_families: tuple[str, ...]) -> None:
    if not header_families:
        return
    key = ",".join(header_families)
    now = time.monotonic()
    count, window_start = _untrusted_forwarding_log_state.get(key, (0, now))
    if now - window_start >= _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
        count = 0
        window_start = now
    count += 1
    _untrusted_forwarding_log_state[key] = (count, window_start)
    if count > _UNTRUSTED_FORWARDING_LOG_LIMIT:
        return
    _logger.info(
        "Admin login ignored forwarding headers from untrusted peer",
        extra={
            "resolution_path": "untrusted_peer_headers_ignored",
            "header_family_count": len(header_families),
            "header_families": key,
            "sampled_event_index": count,
        },
    )


def _record_resolution(path: str, *, hop_count: int | None = None) -> None:
    extra: dict[str, int | str] = {"resolution_path": path}
    if hop_count is not None:
        extra["hop_count"] = hop_count
    _logger.debug("Admin login client source resolved", extra=extra)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer = immediate_peer_host(request)
    peer_normalized = normalize_client_source(peer)
    peer_source = _source_from_peer(peer)
    trusted_networks = settings.admin_trusted_proxy_networks

    if not settings.admin_trust_proxy_headers:
        _record_resolution("direct_peer_no_trust")
        return ClientSourceResolution(source=peer_source, path="direct_peer_no_trust")

    if peer_normalized is None and peer is None:
        header_families = _forwarding_header_families(request)
        if header_families:
            _record_untrusted_forwarding_attempt(header_families)
        _record_resolution("missing_peer")
        return ClientSourceResolution(source="unknown", path="missing_peer")

    if peer_normalized is None or not _ip_in_trusted_networks(peer_normalized, trusted_networks):
        header_families = _forwarding_header_families(request)
        if header_families:
            _record_untrusted_forwarding_attempt(header_families)
        _record_resolution("untrusted_peer_headers_ignored")
        return ClientSourceResolution(
            source=peer_source,
            path="untrusted_peer_headers_ignored",
        )

    x_forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if x_forwarded_for:
        hops = _split_forwarded_for(x_forwarded_for)
        hops.append(peer_normalized)
        resolved, path = _resolve_from_hops(hops, trusted_networks=trusted_networks)
        if resolved is not None:
            _record_resolution(path, hop_count=len(hops))
            return ClientSourceResolution(source=resolved, path=path)
        _record_resolution(path)
        return ClientSourceResolution(source=peer_source, path=path)

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        hops = _parse_forwarded_header(forwarded)
        hops.append(peer_normalized or peer_source)
        resolved, path = _resolve_from_hops(hops, trusted_networks=trusted_networks)
        if resolved is not None:
            _record_resolution(path, hop_count=len(hops))
            return ClientSourceResolution(source=resolved, path=path)
        _record_resolution(path)
        return ClientSourceResolution(source=peer_source, path=path)

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        _record_untrusted_forwarding_attempt(("cf_connecting_ip",))
        _record_resolution("cf_connecting_ip_ignored_without_chain")
        return ClientSourceResolution(
            source=peer_source,
            path="cf_connecting_ip_ignored_without_chain",
        )

    _record_resolution("trusted_peer_direct")
    return ClientSourceResolution(source=peer_source, path="trusted_peer_direct")


def reset_client_source_telemetry_for_tests() -> None:
    """Clear rate-limited telemetry counters (tests only)."""
    _untrusted_forwarding_log_state.clear()
