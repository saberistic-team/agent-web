"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings
from app.proxy_trust import DEFAULT_UVICORN_FORWARDED_ALLOW_IPS

_logger = logging.getLogger(__name__)

# Conservative bound on forwarded hop count (comma-separated XFF elements).
MAX_FORWARD_CHAIN_LENGTH = 10

# Sample invalid/untrusted forwarding telemetry at most once per interval.
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_telemetry_lock = threading.Lock()
_telemetry_last_logged: dict[str, float] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) for deterministic limiter keys."""
    candidate = raw.strip()
    if not candidate:
        return None

    # Bracketed IPv6 with optional port: [::1]:8080
    bracket_match = re.match(r"^\[([^\]]+)\](?::\d+)?$", candidate)
    if bracket_match:
        candidate = bracket_match.group(1)

    # IPv4 host:port — only split when the left side is dotted decimal.
    if candidate.count(":") == 1 and "." in candidate.split(":", 1)[0]:
        host, _port = candidate.rsplit(":", 1)
        candidate = host

    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return addr.compressed


def peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() if host else None


def is_trusted_proxy_address(host: str, trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    normalized = normalize_client_address(host)
    if normalized is None:
        return False
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(addr in network for network in trusted_networks)


def _split_forwarded_for(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _parse_forwarded_header(header_value: str) -> str | None:
    """Extract the leftmost ``for=`` target from an RFC 7239 Forwarded header."""
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        for segment in entry.split(";"):
            segment = segment.strip()
            if not segment.lower().startswith("for="):
                continue
            value = segment[4:].strip().strip('"')
            if value.lower() == "unknown":
                return None
            if value.startswith("["):
                end = value.find("]")
                if end == -1:
                    return None
                return normalize_client_address(value[1:end])
            host = value
            if value.count(":") == 1 and "." in value.split(":", 1)[0]:
                host = value.split(":", 1)[0]
            return normalize_client_address(host)
    return None


def _resolve_from_xff_chain(
    chain: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)

    index = len(normalized_chain) - 1
    while index >= 0 and is_trusted_proxy_address(normalized_chain[index], trusted_networks):
        index -= 1

    if index < 0:
        return None
    return normalized_chain[index]


def _log_resolution_telemetry(path: str) -> None:
    extra = {"admin_login_source_path": path}
    if path in {"malformed_header", "chain_too_long", "untrusted_peer_ignored_headers"}:
        _sampled_warning("admin_login_source_forwarding_rejected", extra)
        return
    _logger.debug("admin_login_source_resolved", extra=extra)


def _sampled_warning(event: str, extra: dict[str, str]) -> None:
    now = time.monotonic()
    with _telemetry_lock:
        last = _telemetry_last_logged.get(event, 0.0)
        if now - last < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            return
        _telemetry_last_logged[event] = now
    _logger.warning(event, extra=extra)


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled telemetry counters (tests only)."""
    with _telemetry_lock:
        _telemetry_last_logged.clear()


def resolve_admin_login_client_source(request: Request, settings: Settings) -> str:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_IPS`` and ``ADMIN_TRUST_PROXY_HEADERS`` is enabled.
    """
    resolution = _resolve_admin_login_client_source(request, settings)
    _log_resolution_telemetry(resolution.path)
    return resolution.source


def _resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    peer = peer_host(request)
    normalized_peer = normalize_client_address(peer) if peer else None
    peer_source = normalized_peer or (peer.strip().lower() if peer else None)

    if peer_source is None:
        return ClientSourceResolution(source="unknown", path="missing_peer")

    trusted_networks = settings.admin_trusted_proxy_networks

    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(source=peer_source, path="direct_peer_proxy_trust_disabled")

    if not is_trusted_proxy_address(normalized_peer or "", trusted_networks):
        has_forwarding_headers = any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        )
        if has_forwarding_headers:
            return ClientSourceResolution(
                source=peer_source,
                path="untrusted_peer_ignored_headers",
            )
        return ClientSourceResolution(source=peer_source, path="direct_peer")

    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    if cf_header:
        cf_source = normalize_client_address(cf_header)
        if cf_source is not None:
            return ClientSourceResolution(source=cf_source, path="cf_connecting_ip")
        return ClientSourceResolution(source=peer_source, path="malformed_header")

    xff_header = request.headers.get("x-forwarded-for", "").strip()
    if xff_header:
        chain = _split_forwarded_for(xff_header)
        if not chain:
            return ClientSourceResolution(source=peer_source, path="malformed_header")
        if len(chain) > MAX_FORWARD_CHAIN_LENGTH:
            return ClientSourceResolution(source=peer_source, path="chain_too_long")
        xff_source = _resolve_from_xff_chain(chain, trusted_networks)
        if xff_source is None:
            return ClientSourceResolution(source=peer_source, path="malformed_header")
        return ClientSourceResolution(source=xff_source, path="xff_trusted_chain")

    forwarded_header = request.headers.get("forwarded", "").strip()
    if forwarded_header:
        forwarded_source = _parse_forwarded_header(forwarded_header)
        if forwarded_source is None:
            return ClientSourceResolution(source=peer_source, path="malformed_header")
        return ClientSourceResolution(source=forwarded_source, path="forwarded_header")

    return ClientSourceResolution(source=peer_source, path="direct_peer")
