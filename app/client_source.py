"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from app.config import Settings
from app.proxy_networks import (
    MAX_FORWARDING_CHAIN_LENGTH,
    address_in_networks,
    format_normalized_ip,
    normalize_ip_address,
    parse_forwarded_header,
    split_forwarded_for,
)

_logger = logging.getLogger(__name__)

_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0

_invalid_forwarding_lock = Lock()
_invalid_forwarding_last_logged_at = 0.0


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved admin login client source without exposing raw forwarding data."""

    source: str
    path: str
    invalid_forwarding: bool = False


def _resolve_from_xff_chain(
    chain: list[str],
    *,
    trusted_proxy_networks: tuple,
    trusted_forwarder_networks: tuple,
) -> str | None:
    if not chain or len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    normalized_chain = []
    for token in chain:
        parsed = normalize_ip_address(token)
        if parsed is None:
            return None
        normalized_chain.append(parsed)

    for address in reversed(normalized_chain):
        if address_in_networks(address, trusted_proxy_networks):
            continue
        if address_in_networks(address, trusted_forwarder_networks):
            continue
        return format_normalized_ip(address)
    return None


def _chain_contains_forwarder(
    chain: list[str],
    *,
    trusted_forwarder_networks: tuple,
) -> bool:
    for token in chain:
        parsed = normalize_ip_address(token)
        if parsed is None:
            continue
        if address_in_networks(parsed, trusted_forwarder_networks):
            return True
    return False


def _maybe_log_invalid_forwarding(path: str) -> None:
    global _invalid_forwarding_last_logged_at
    now = time.monotonic()
    with _invalid_forwarding_lock:
        if now - _invalid_forwarding_last_logged_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _invalid_forwarding_last_logged_at = now
    _logger.warning(
        "Admin login client source rejected forwarding headers",
        extra={"client_source_path": path},
    )


def _maybe_log_resolution(path: str, *, invalid_forwarding: bool) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={
            "client_source_path": path,
            "invalid_forwarding": invalid_forwarding,
        },
    )
    if invalid_forwarding:
        _maybe_log_invalid_forwarding(path)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain: public client → Cloudflare edge → Render proxy → Uvicorn.

    Forwarding headers are ignored unless the immediate TCP peer is a configured
    trusted proxy. When trusted, ``X-Forwarded-For`` is parsed right-to-left,
    skipping trusted proxy and Cloudflare forwarder hops. ``CF-Connecting-IP`` is
    accepted only when the peer is trusted and the chain proves a Cloudflare hop.
    """
    peer_host = request.client.host if request.client is not None else None
    peer_address = normalize_ip_address(peer_host) if peer_host else None

    if peer_address is None:
        if peer_host:
            _maybe_log_resolution("direct_peer", invalid_forwarding=False)
            return ClientSourceResolution(
                source=peer_host.strip().lower(),
                path="direct_peer",
            )
        _maybe_log_resolution("unknown_peer", invalid_forwarding=False)
        return ClientSourceResolution(source="unknown", path="unknown_peer")

    peer_source = format_normalized_ip(peer_address)
    trusted_proxy_networks = settings.admin_trusted_proxy_networks
    trusted_forwarder_networks = settings.admin_trusted_forwarder_networks

    if not trusted_proxy_networks or not address_in_networks(
        peer_address, trusted_proxy_networks
    ):
        _maybe_log_resolution("direct_peer", invalid_forwarding=False)
        return ClientSourceResolution(source=peer_source, path="direct_peer")

    xff_raw = request.headers.get("x-forwarded-for", "")
    xff_chain = split_forwarded_for(xff_raw) if xff_raw else []
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    forwarded_raw = request.headers.get("forwarded", "")

    invalid_forwarding = False
    if xff_raw and not xff_chain:
        invalid_forwarding = True
    if len(xff_chain) > MAX_FORWARDING_CHAIN_LENGTH:
        invalid_forwarding = True
        xff_chain = []

    cf_path_verified = bool(xff_chain) and _chain_contains_forwarder(
        xff_chain,
        trusted_forwarder_networks=trusted_forwarder_networks,
    )

    if cf_connecting_ip and cf_path_verified:
        cf_address = normalize_ip_address(cf_connecting_ip)
        if cf_address is not None:
            resolution = ClientSourceResolution(
                source=format_normalized_ip(cf_address),
                path="cf_connecting_ip",
                invalid_forwarding=invalid_forwarding,
            )
            _maybe_log_resolution(resolution.path, invalid_forwarding=invalid_forwarding)
            return resolution
        invalid_forwarding = True

    if xff_chain:
        resolved = _resolve_from_xff_chain(
            xff_chain,
            trusted_proxy_networks=trusted_proxy_networks,
            trusted_forwarder_networks=trusted_forwarder_networks,
        )
        if resolved is not None:
            resolution = ClientSourceResolution(
                source=resolved,
                path="xff_right_to_left",
                invalid_forwarding=invalid_forwarding,
            )
            _maybe_log_resolution(resolution.path, invalid_forwarding=invalid_forwarding)
            return resolution
        invalid_forwarding = True

    if forwarded_raw:
        forwarded_chain = parse_forwarded_header(forwarded_raw)
        if forwarded_chain and len(forwarded_chain) <= MAX_FORWARDING_CHAIN_LENGTH:
            resolved = _resolve_from_xff_chain(
                forwarded_chain,
                trusted_proxy_networks=trusted_proxy_networks,
                trusted_forwarder_networks=trusted_forwarder_networks,
            )
            if resolved is not None:
                resolution = ClientSourceResolution(
                    source=resolved,
                    path="forwarded_header",
                    invalid_forwarding=invalid_forwarding,
                )
                _maybe_log_resolution(resolution.path, invalid_forwarding=invalid_forwarding)
                return resolution
        invalid_forwarding = True

    resolution = ClientSourceResolution(
        source=peer_source,
        path="trusted_peer_fallback",
        invalid_forwarding=invalid_forwarding,
    )
    _maybe_log_resolution(resolution.path, invalid_forwarding=invalid_forwarding)
    return resolution


def client_ip(request: Request, settings: Settings) -> str:
    """Return normalized client source string for admin login limiter buckets."""
    return resolve_admin_login_client_source(request, settings).source
