"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from app.config import Settings
from app.ip_trust import address_in_trusted_networks, normalize_client_address

_logger = logging.getLogger(__name__)

# Conservative upper bound; overlong chains are treated as untrusted forwarding data.
MAX_FORWARDING_CHAIN_LENGTH: Final[int] = 32

# Sample at most one invalid-forwarding telemetry event per interval per process.
_INVALID_FORWARDING_SAMPLE_INTERVAL_SECONDS: Final[float] = 60.0
_last_invalid_forwarding_logged_at: float = 0.0

# Deployment fingerprint surfaced by /health (no raw addresses).
CLIENT_SOURCE_TRUST_MODEL: Final[str] = "verified-proxy-hop-v1"

# Bounded telemetry path identifiers (no raw IP/header values).
PATH_DIRECT_PEER: Final[str] = "direct_peer"
PATH_MISSING_PEER: Final[str] = "missing_peer"
PATH_TRUSTED_XFF_RTL: Final[str] = "trusted_xff_right_to_left"
PATH_TRUSTED_FORWARDED: Final[str] = "trusted_forwarded"
PATH_TRUSTED_CF_CONNECTING_IP: Final[str] = "trusted_cf_connecting_ip"
PATH_TRUSTED_PEER_NO_FORWARDING: Final[str] = "trusted_peer_no_forwarding"
PATH_MALFORMED_FORWARDING: Final[str] = "malformed_forwarding"

_FORWARDED_FOR_TOKEN_RE = re.compile(r"for=(?P<value>[^;,\s]+|\"[^\"]+\")", re.IGNORECASE)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and telemetry path."""

    source: str
    path: str
    header_family: str
    chain_length_bucket: str
def immediate_peer_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _chain_length_bucket(length: int) -> str:
    if length <= 0:
        return "0"
    if length == 1:
        return "1"
    if length <= 3:
        return "2-3"
    if length <= 8:
        return "4-8"
    return "9+"


def _split_forwarding_list(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _parse_x_forwarded_for(header_value: str) -> list[str]:
    hops: list[str] = []
    for element in _split_forwarding_list(header_value):
        normalized = normalize_client_address(element)
        if normalized is None:
            return []
        hops.append(normalized)
    return hops


def _parse_forwarded_header(header_value: str) -> list[str]:
    hops: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN_RE.search(entry)
        if match is None:
            continue
        token = match.group("value").strip().strip('"')
        normalized = normalize_client_address(token)
        if normalized is None:
            return []
        hops.append(normalized)
    return hops


def _resolve_from_trusted_chain(
    hops: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping trusted proxy hops."""
    for hop in reversed(hops):
        if not address_in_trusted_networks(hop, trusted_networks):
            return hop
    return None


def _log_invalid_forwarding(reason: str) -> None:
    global _last_invalid_forwarding_logged_at
    now = time.monotonic()
    if now - _last_invalid_forwarding_logged_at < _INVALID_FORWARDING_SAMPLE_INTERVAL_SECONDS:
        return
    _last_invalid_forwarding_logged_at = now
    _logger.info(
        "Admin login forwarding headers ignored",
        extra={
            "source_resolution_path": PATH_MALFORMED_FORWARDING,
            "invalid_forwarding_reason": reason,
        },
    )


def _log_resolution(resolution: ClientSourceResolution) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={
            "source_resolution_path": resolution.path,
            "forwarding_header_family": resolution.header_family,
            "chain_length_bucket": resolution.chain_length_bucket,
        },
    )


def _opaque_peer_fallback(peer_raw: str) -> str:
    stripped = peer_raw.strip()
    if not stripped:
        return "unknown"
    return stripped.lower()


def _trusted_peer_fallback(
    peer_normalized: str | None,
    path: str,
) -> ClientSourceResolution:
    if peer_normalized is not None:
        resolution = ClientSourceResolution(
            source=peer_normalized,
            path=path,
            header_family="none",
            chain_length_bucket="0",
        )
    else:
        resolution = ClientSourceResolution(
            source="unknown",
            path=PATH_MISSING_PEER,
            header_family="none",
            chain_length_bucket="0",
        )
    _log_resolution(resolution)
    return resolution


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for rate limiting.

    Production chain (saberistic.com):

    ``Client → Cloudflare edge → Render load balancer → Uvicorn``

    Forwarding headers are parsed only when the immediate TCP peer is a member of
    ``ADMIN_TRUSTED_PROXY_IPS``. Untrusted peers always map to the direct peer
    address so spoofed ``X-Forwarded-For``, ``Forwarded``, and ``CF-Connecting-IP``
    values cannot influence the limiter key.
    """
    trusted_networks = settings.admin_trusted_proxy_networks
    peer_raw = immediate_peer_host(request)
    peer_normalized = normalize_client_address(peer_raw)
    peer_trusted = (
        peer_normalized is not None
        and trusted_networks
        and address_in_trusted_networks(peer_normalized, trusted_networks)
    )

    if not peer_trusted:
        if peer_normalized is not None:
            resolution = ClientSourceResolution(
                source=peer_normalized,
                path=PATH_DIRECT_PEER,
                header_family="none",
                chain_length_bucket="0",
            )
        elif peer_raw:
            resolution = ClientSourceResolution(
                source=_opaque_peer_fallback(peer_raw),
                path=PATH_DIRECT_PEER,
                header_family="none",
                chain_length_bucket="0",
            )
        else:
            resolution = ClientSourceResolution(
                source="unknown",
                path=PATH_MISSING_PEER,
                header_family="none",
                chain_length_bucket="0",
            )
        _log_resolution(resolution)
        return resolution

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        if len(xff_header) > 2048:
            _log_invalid_forwarding("xff_overlong")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        hops = _parse_x_forwarded_for(xff_header)
        if not hops:
            _log_invalid_forwarding("xff_malformed")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        if len(hops) > MAX_FORWARDING_CHAIN_LENGTH:
            _log_invalid_forwarding("xff_chain_too_long")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        client = _resolve_from_trusted_chain(hops, trusted_networks=trusted_networks)
        if client is not None:
            resolution = ClientSourceResolution(
                source=client,
                path=PATH_TRUSTED_XFF_RTL,
                header_family="xff",
                chain_length_bucket=_chain_length_bucket(len(hops)),
            )
            _log_resolution(resolution)
            return resolution

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        if len(forwarded_header) > 2048:
            _log_invalid_forwarding("forwarded_overlong")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        hops = _parse_forwarded_header(forwarded_header)
        if not hops:
            _log_invalid_forwarding("forwarded_malformed")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        if len(hops) > MAX_FORWARDING_CHAIN_LENGTH:
            _log_invalid_forwarding("forwarded_chain_too_long")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        client = _resolve_from_trusted_chain(hops, trusted_networks=trusted_networks)
        if client is not None:
            resolution = ClientSourceResolution(
                source=client,
                path=PATH_TRUSTED_FORWARDED,
                header_family="forwarded",
                chain_length_bucket=_chain_length_bucket(len(hops)),
            )
            _log_resolution(resolution)
            return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        client = normalize_client_address(cf_header)
        if client is None:
            _log_invalid_forwarding("cf_connecting_ip_malformed")
            return _trusted_peer_fallback(peer_normalized, PATH_MALFORMED_FORWARDING)
        resolution = ClientSourceResolution(
            source=client,
            path=PATH_TRUSTED_CF_CONNECTING_IP,
            header_family="cf_connecting_ip",
            chain_length_bucket="1",
        )
        _log_resolution(resolution)
        return resolution

    return _trusted_peer_fallback(peer_normalized, PATH_TRUSTED_PEER_NO_FORWARDING)
