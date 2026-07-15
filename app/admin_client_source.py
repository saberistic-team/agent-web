"""Resolve admin login client source through a verified proxy boundary."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import Request

from app.config import Settings
from app.trusted_proxy_boundary import (
    CLOUDFLARE_IPV4_CIDRS,
    CLOUDFLARE_IPV6_CIDRS,
    TrustedProxyBoundary,
    normalize_ip_address,
    parse_host_list,
    parse_host_port,
)

_logger = logging.getLogger(__name__)

# Sample untrusted forwarding-header telemetry (no raw addresses).
_UNTRUSTED_HEADER_SAMPLE_RATE = 100
_untrusted_header_counter = 0
_untrusted_header_last_log = 0.0
_UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS = 60.0


class ClientSourceResolutionPath(str, Enum):
    DIRECT_PEER = "direct_peer"
    FORWARDED_FOR = "forwarded_for"
    FORWARDED = "forwarded"
    CF_CONNECTING_IP = "cf_connecting_ip"
    UNKNOWN = "unknown"
    UNTRUSTED_HEADERS = "untrusted_headers"


@dataclass(frozen=True)
class ClientSourceResolution:
    source: str
    path: ClientSourceResolutionPath


def build_trusted_proxy_boundary(settings: Settings) -> TrustedProxyBoundary:
    forwarding_cidrs = list(settings.admin_trusted_forwarding_cidrs)
    if settings.admin_trust_cloudflare_forwarding:
        forwarding_cidrs.extend(CLOUDFLARE_IPV4_CIDRS)
        forwarding_cidrs.extend(CLOUDFLARE_IPV6_CIDRS)
    return TrustedProxyBoundary(
        immediate_peer_cidrs=settings.admin_trusted_proxy_cidrs,
        forwarding_chain_cidrs=tuple(forwarding_cidrs),
    )


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _normalized_peer(request: Request) -> str:
    peer = _peer_host(request)
    if peer is None:
        return "unknown"
    normalized = normalize_ip_address(peer)
    if normalized is not None:
        return normalized
    stripped = peer.strip()
    return stripped or "unknown"


def _has_forwarding_headers(request: Request) -> bool:
    header_names = {
        b"x-forwarded-for",
        b"forwarded",
        b"cf-connecting-ip",
        b"x-real-ip",
    }
    for name, _value in request.headers.raw:
        if name.lower() in header_names:
            return True
    return False


def _maybe_log_untrusted_headers(*, path: ClientSourceResolutionPath) -> None:
    global _untrusted_header_counter, _untrusted_header_last_log

    _untrusted_header_counter += 1
    now = time.monotonic()
    should_log = (
        _untrusted_header_counter % _UNTRUSTED_HEADER_SAMPLE_RATE == 0
        or now - _untrusted_header_last_log >= _UNTRUSTED_HEADER_LOG_INTERVAL_SECONDS
    )
    if not should_log:
        return
    _untrusted_header_last_log = now
    _logger.info(
        "Admin login client source ignored forwarding headers",
        extra={
            "resolution_path": path.value,
            "sampled": True,
            "sample_count": _untrusted_header_counter,
        },
    )


def _log_resolution(path: ClientSourceResolutionPath) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    boundary = build_trusted_proxy_boundary(settings)
    peer = _peer_host(request)
    normalized_peer = _normalized_peer(request)

    if not boundary.immediate_peer_trusted(peer):
        if _has_forwarding_headers(request):
            _maybe_log_untrusted_headers(path=ClientSourceResolutionPath.UNTRUSTED_HEADERS)
        path = (
            ClientSourceResolutionPath.UNKNOWN
            if peer is None
            else ClientSourceResolutionPath.DIRECT_PEER
        )
        _log_resolution(path)
        return ClientSourceResolution(source=normalized_peer, path=path)

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    if x_forwarded_for:
        resolved = boundary.client_from_forwarded_for(x_forwarded_for)
        if resolved:
            _log_resolution(ClientSourceResolutionPath.FORWARDED_FOR)
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.FORWARDED_FOR,
            )

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        resolved = boundary.client_from_forwarded_header(forwarded)
        if resolved:
            _log_resolution(ClientSourceResolutionPath.FORWARDED)
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourceResolutionPath.FORWARDED,
            )

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip and settings.admin_trust_cloudflare_forwarding:
        chain_hosts = [
            host
            for token in parse_host_list(x_forwarded_for)
            for host in [normalize_ip_address(parse_host_port(token)[0])]
            if host
        ]
        if boundary.cloudflare_hop_present(chain_hosts):
            normalized_cf = normalize_ip_address(cf_connecting_ip.strip())
            if normalized_cf:
                _log_resolution(ClientSourceResolutionPath.CF_CONNECTING_IP)
                return ClientSourceResolution(
                    source=normalized_cf,
                    path=ClientSourceResolutionPath.CF_CONNECTING_IP,
                )

    if normalized_peer != "unknown":
        _log_resolution(ClientSourceResolutionPath.DIRECT_PEER)
        return ClientSourceResolution(
            source=normalized_peer,
            path=ClientSourceResolutionPath.DIRECT_PEER,
        )

    _log_resolution(ClientSourceResolutionPath.UNKNOWN)
    return ClientSourceResolution(
        source="unknown",
        path=ClientSourceResolutionPath.UNKNOWN,
    )


def client_source_trust_health(settings: Settings) -> dict[str, Any]:
    """Non-sensitive deployment verification payload for /health."""
    return {
        "immediate_peer_cidrs_configured": bool(settings.admin_trusted_proxy_cidrs),
        "forwarding_cidrs_configured": bool(settings.admin_trusted_forwarding_cidrs),
        "cloudflare_forwarding_enabled": settings.admin_trust_cloudflare_forwarding,
        "legacy_proxy_header_flag": settings.admin_trust_proxy_headers,
    }


def trust_model_summary(settings: Settings) -> dict[str, Any]:
    """Alias for deployment verification tooling."""
    return client_source_trust_health(settings)


def reset_untrusted_header_telemetry_for_tests() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _untrusted_header_counter, _untrusted_header_last_log
    _untrusted_header_counter = 0
    _untrusted_header_last_log = 0.0


def reset_client_source_telemetry_for_tests() -> None:
    """Alias for test harness compatibility."""
    reset_untrusted_header_telemetry_for_tests()
