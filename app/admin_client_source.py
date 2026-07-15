"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings
from app.proxy_trust_constants import (
    DEFAULT_MAX_FORWARDED_CHAIN_LENGTH,
    DEFAULT_TRUSTED_FORWARDED_NETWORKS,
    DEFAULT_TRUSTED_IMMEDIATE_PEER_NETWORKS,
)

_logger = logging.getLogger(__name__)

# Sample at most one invalid/untrusted forwarding telemetry event per interval.
_INVALID_FORWARDING_TELEMETRY_INTERVAL_SECONDS = 60.0
_last_invalid_forwarding_telemetry_at = 0.0


class ClientSourceResolutionPath(StrEnum):
    DIRECT_PEER = "direct_peer"
    MISSING_PEER = "missing_peer"
    FORWARDED_TRUSTED = "forwarded_trusted"
    FORWARDED_UNTRUSTED_PEER = "forwarded_untrusted_peer"
    FORWARDED_MALFORMED = "forwarded_malformed"
    FORWARDED_TOO_LONG = "forwarded_too_long"
    FORWARDED_EMPTY = "forwarded_empty"
    FORWARDED_ALL_TRUSTED = "forwarded_all_trusted"
    CF_CONNECTING_IP_REJECTED = "cf_connecting_ip_rejected"
    FORWARDED_HEADER_CONFLICT = "forwarded_header_conflict"


@dataclass(frozen=True)
class ClientSourceResolution:
    source: str
    path: ClientSourceResolutionPath


class _TrustedNetworks:
    def __init__(self, networks: Iterable[str]) -> None:
        self._literals: set[str] = set()
        self._hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in networks:
            value = item.strip()
            if not value:
                continue
            if "/" in value:
                try:
                    self._networks.append(ipaddress.ip_network(value, strict=False))
                except ValueError:
                    self._literals.add(value)
            else:
                try:
                    self._hosts.add(ipaddress.ip_address(value))
                except ValueError:
                    self._literals.add(value)

    def contains(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host.strip())
        except ValueError:
            return host.strip() in self._literals
        if ip in self._hosts:
            return True
        return any(ip in network for network in self._networks)


def _parse_host_port(value: str) -> tuple[str, int]:
    host = value.strip()
    if not host:
        return "", 0
    if host.startswith("[") and "]" in host:
        literal, _, remainder = host.partition("]")
        host_part = literal[1:]
        port = 0
        if remainder.startswith(":"):
            try:
                port = int(remainder[1:])
            except ValueError:
                port = 0
        return host_part, port
    if host.count(":") == 1 and "." in host:
        left, _, right = host.partition(":")
        try:
            return left, int(right)
        except ValueError:
            return host, 0
    return host, 0


def _split_forwarded_for(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


_FORWARDED_FOR_RE = re.compile(
    r"""for=(?:"
        (?P<quoted>[^"]+)
        "|
        (?P<token>[^;,\s]+)
    )""",
    re.VERBOSE,
)


def _parse_forwarded_header(raw: str) -> list[str]:
    values: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        candidate = match.group("quoted") or match.group("token") or ""
        candidate = candidate.strip()
        if candidate.casefold() == "unknown":
            continue
        if candidate:
            values.append(candidate)
    return values


def normalize_client_source(value: str | None) -> str | None:
    if value is None:
        return None
    host, _port = _parse_host_port(value.strip())
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return format(ip.compressed if isinstance(ip, ipaddress.IPv6Address) else ip)


def _resolve_from_chain(
    chain: list[str],
    *,
    trusted_forwarded: _TrustedNetworks,
    max_chain_length: int,
) -> tuple[str | None, ClientSourceResolutionPath]:
    if not chain:
        return None, ClientSourceResolutionPath.FORWARDED_EMPTY
    if len(chain) > max_chain_length:
        return None, ClientSourceResolutionPath.FORWARDED_TOO_LONG

    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_client_source(hop)
        if normalized is None:
            return None, ClientSourceResolutionPath.FORWARDED_MALFORMED
        normalized_chain.append(normalized)

    for hop in reversed(normalized_chain):
        if trusted_forwarded.contains(hop):
            continue
        return hop, ClientSourceResolutionPath.FORWARDED_TRUSTED

    return None, ClientSourceResolutionPath.FORWARDED_ALL_TRUSTED


def _chain_contains_trusted_forwarded_hop(
    chain: list[str],
    *,
    trusted_forwarded: _TrustedNetworks,
) -> bool:
    for hop in chain:
        normalized = normalize_client_source(hop)
        if normalized and trusted_forwarded.contains(normalized):
            return True
    return False


def _maybe_emit_invalid_forwarding_telemetry(path: ClientSourceResolutionPath) -> None:
    global _last_invalid_forwarding_telemetry_at
    if path in {
        ClientSourceResolutionPath.FORWARDED_TRUSTED,
        ClientSourceResolutionPath.DIRECT_PEER,
        ClientSourceResolutionPath.MISSING_PEER,
    }:
        return
    now = time.monotonic()
    if now - _last_invalid_forwarding_telemetry_at < _INVALID_FORWARDING_TELEMETRY_INTERVAL_SECONDS:
        return
    _last_invalid_forwarding_telemetry_at = now
    _logger.info(
        "Admin login forwarding header rejected",
        extra={"resolution_path": path.value},
    )


def emit_client_source_resolution_telemetry(path: ClientSourceResolutionPath) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={"resolution_path": path.value},
    )
    _maybe_emit_invalid_forwarding_telemetry(path)


@lru_cache(maxsize=8)
def _trusted_networks_from_key(
    immediate_key: tuple[str, ...],
    forwarded_key: tuple[str, ...],
) -> tuple[_TrustedNetworks, _TrustedNetworks]:
    return (
        _TrustedNetworks(immediate_key),
        _TrustedNetworks(forwarded_key),
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    immediate_peer = request.client.host if request.client is not None else None
    immediate_key = settings.admin_login_trusted_immediate_peer_networks
    forwarded_key = settings.admin_login_trusted_forwarded_networks
    trusted_immediate, trusted_forwarded = _trusted_networks_from_key(
        immediate_key,
        forwarded_key,
    )

    if immediate_peer is None:
        return ClientSourceResolution("unknown", ClientSourceResolutionPath.MISSING_PEER)

    normalized_peer = normalize_client_source(immediate_peer)
    if normalized_peer is None:
        literal_peer = immediate_peer.strip()
        if not literal_peer:
            return ClientSourceResolution("unknown", ClientSourceResolutionPath.MISSING_PEER)
        normalized_peer = literal_peer

    if not settings.admin_login_trust_forwarded_headers:
        return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)

    if not trusted_immediate.contains(normalized_peer):
        return ClientSourceResolution(
            normalized_peer,
            ClientSourceResolutionPath.FORWARDED_UNTRUSTED_PEER,
        )

    xff_raw = request.headers.get("x-forwarded-for", "")
    forwarded_raw = request.headers.get("forwarded", "")
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")

    xff_chain = _split_forwarded_for(xff_raw) if xff_raw else []
    forwarded_chain = _parse_forwarded_header(forwarded_raw) if forwarded_raw else []

    xff_client: str | None = None
    xff_path = ClientSourceResolutionPath.FORWARDED_EMPTY
    if xff_chain:
        xff_client, xff_path = _resolve_from_chain(
            xff_chain,
            trusted_forwarded=trusted_forwarded,
            max_chain_length=settings.admin_login_max_forwarded_chain_length,
        )

    forwarded_client: str | None = None
    forwarded_path = ClientSourceResolutionPath.FORWARDED_EMPTY
    if forwarded_chain:
        forwarded_client, forwarded_path = _resolve_from_chain(
            forwarded_chain,
            trusted_forwarded=trusted_forwarded,
            max_chain_length=settings.admin_login_max_forwarded_chain_length,
        )

    if (
        xff_client is not None
        and forwarded_client is not None
        and xff_client != forwarded_client
    ):
        return ClientSourceResolution(
            xff_client,
            ClientSourceResolutionPath.FORWARDED_HEADER_CONFLICT,
        )

    for client, path in (
        (xff_client, xff_path),
        (forwarded_client, forwarded_path),
    ):
        if client is not None:
            return ClientSourceResolution(client, path)

    if cf_connecting_ip:
        cf_client = normalize_client_source(cf_connecting_ip)
        if cf_client is None:
            return ClientSourceResolution(
                normalized_peer,
                ClientSourceResolutionPath.CF_CONNECTING_IP_REJECTED,
            )
        combined_chain = xff_chain or forwarded_chain
        if combined_chain and _chain_contains_trusted_forwarded_hop(
            combined_chain,
            trusted_forwarded=trusted_forwarded,
        ):
            return ClientSourceResolution(cf_client, ClientSourceResolutionPath.FORWARDED_TRUSTED)
        return ClientSourceResolution(
            normalized_peer,
            ClientSourceResolutionPath.CF_CONNECTING_IP_REJECTED,
        )

    if xff_chain or forwarded_chain:
        return ClientSourceResolution(normalized_peer, xff_path if xff_chain else forwarded_path)

    return ClientSourceResolution(normalized_peer, ClientSourceResolutionPath.DIRECT_PEER)


def reset_client_source_telemetry_for_tests() -> None:
    global _last_invalid_forwarding_telemetry_at
    _last_invalid_forwarding_telemetry_at = 0.0
    _trusted_networks_from_key.cache_clear()
