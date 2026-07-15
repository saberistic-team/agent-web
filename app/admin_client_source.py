"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_MAX_HEADER_BYTES = 4096
_TELEMETRY_SAMPLE_SECONDS = 60.0
_telemetry_lock = Lock()
_last_telemetry_at: dict[str, float] = {}

DEFAULT_TRUSTED_PROXY_IPS = (
    "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
)


class ClientSourcePath(StrEnum):
    DIRECT_PEER = "direct_peer"
    UNTRUSTED_PEER = "untrusted_peer"
    X_FORWARDED_FOR = "x_forwarded_for"
    FORWARDED_HEADER = "forwarded"
    CF_CONNECTING_IP = "cf_connecting_ip"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClientSourceResolution:
    source: str
    path: ClientSourcePath


@dataclass(frozen=True)
class TrustedProxyBoundary:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    literals: tuple[str, ...]


def reset_client_source_telemetry_for_tests() -> None:
    """Clear rate-limited telemetry state (tests only)."""
    with _telemetry_lock:
        _last_telemetry_at.clear()


def parse_trusted_proxy_networks(spec: str) -> TrustedProxyBoundary:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    literals: list[str] = []
    for part in spec.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            else:
                address = ipaddress.ip_address(candidate)
                networks.append(
                    ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False)
                )
        except ValueError:
            literals.append(candidate.lower())
    return TrustedProxyBoundary(tuple(networks), tuple(literals))


def _strip_port(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("["):
        closing = value.find("]")
        if closing != -1:
            return value[1:closing]
        return value
    if value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        if host.count(".") == 3 and port.isdigit():
            return host
    return value


def normalize_client_address(value: str) -> str | None:
    """Normalize IPv4/IPv6 addresses; return ``None`` for missing/invalid input."""
    if not value or len(value) > 256:
        return None
    candidate = _strip_port(value.strip().lower())
    if not candidate:
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def is_trusted_proxy_address(
    address: str,
    boundary: TrustedProxyBoundary,
) -> bool:
    raw = address.strip().lower()
    if raw and raw in boundary.literals:
        return True
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in boundary.networks)


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    raw_host = request.client.host.strip()
    if not raw_host:
        return None
    normalized = normalize_client_address(raw_host)
    if normalized is not None:
        return normalized
    return raw_host.lower()


def split_forwarding_chain(header_value: str) -> list[str]:
    if not header_value or len(header_value) > _MAX_HEADER_BYTES:
        return []
    parts = [segment.strip() for segment in header_value.split(",") if segment.strip()]
    if len(parts) > MAX_FORWARDING_CHAIN_LENGTH:
        return []
    return parts


def _parse_forwarded_for_values(request: Request) -> list[str]:
    return split_forwarding_chain(request.headers.get("x-forwarded-for", ""))


def _parse_forwarded_header_for_values(request: Request) -> list[str]:
    raw = request.headers.get("forwarded", "")
    if not raw or len(raw) > _MAX_HEADER_BYTES:
        return []
    values: list[str] = []
    for element in raw.split(","):
        element = element.strip()
        if not element:
            continue
        for token in element.split(";"):
            token = token.strip()
            if not token.lower().startswith("for="):
                continue
            value = token[4:].strip().strip('"')
            if value.lower() in {"unknown", "[unknown]"}:
                continue
            values.append(value)
    return values


def resolve_from_chain_right_to_left(
    chain: list[str],
    boundary: TrustedProxyBoundary,
) -> str | None:
    if not chain or len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None
    for hop in reversed(chain):
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        if is_trusted_proxy_address(normalized, boundary):
            continue
        return normalized
    return None


def _cloudflare_hop_present(
    chain: list[str],
    cloudflare_boundary: TrustedProxyBoundary,
) -> bool:
    if not cloudflare_boundary.networks and not cloudflare_boundary.literals:
        return False
    return any(
        normalized is not None
        and is_trusted_proxy_address(normalized, cloudflare_boundary)
        for hop in chain
        if (normalized := normalize_client_address(hop)) is not None
    )


def _has_forwarding_headers(request: Request) -> bool:
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
        or request.headers.get("cf-connecting-ip")
    )


def _emit_telemetry(path: ClientSourcePath, *, invalid_forwarding: bool = False) -> None:
    key = f"{path.value}:invalid={invalid_forwarding}"
    now = time.monotonic()
    with _telemetry_lock:
        last = _last_telemetry_at.get(key)
        if last is not None and now - last < _TELEMETRY_SAMPLE_SECONDS:
            return
        _last_telemetry_at[key] = now
    extra = {
        "admin_client_source_path": path.value,
        "invalid_forwarding": invalid_forwarding,
    }
    if invalid_forwarding or path in (
        ClientSourcePath.UNTRUSTED_PEER,
        ClientSourcePath.MALFORMED,
    ):
        _logger.info("Admin login client source resolution", extra=extra)
    else:
        _logger.debug("Admin login client source resolution", extra=extra)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source behind trusted proxies."""
    peer = _immediate_peer(request)
    peer_source = peer or "unknown"

    if not settings.admin_trust_proxy_headers:
        _emit_telemetry(ClientSourcePath.DIRECT_PEER)
        return ClientSourceResolution(peer_source, ClientSourcePath.DIRECT_PEER)

    trusted_boundary = parse_trusted_proxy_networks(settings.admin_trusted_proxy_ips)
    if peer is None or not is_trusted_proxy_address(peer, trusted_boundary):
        _emit_telemetry(
            ClientSourcePath.UNTRUSTED_PEER,
            invalid_forwarding=_has_forwarding_headers(request),
        )
        return ClientSourceResolution(peer_source, ClientSourcePath.UNTRUSTED_PEER)

    xff_chain = _parse_forwarded_for_values(request)
    cloudflare_boundary = (
        parse_trusted_proxy_networks(settings.admin_cloudflare_proxy_cidrs)
        if settings.admin_trust_cloudflare_headers
        else TrustedProxyBoundary((), ())
    )
    combined_boundary = TrustedProxyBoundary(
        trusted_boundary.networks + cloudflare_boundary.networks,
        trusted_boundary.literals + cloudflare_boundary.literals,
    )
    invalid_forwarding = False

    if len(xff_chain) > MAX_FORWARDING_CHAIN_LENGTH:
        _emit_telemetry(ClientSourcePath.MALFORMED, invalid_forwarding=True)
        return ClientSourceResolution("unknown", ClientSourcePath.MALFORMED)

    if settings.admin_trust_cloudflare_headers and (
        cloudflare_boundary.networks or cloudflare_boundary.literals
    ):
        cf_header = request.headers.get("cf-connecting-ip", "")
        if cf_header:
            if _cloudflare_hop_present(xff_chain, cloudflare_boundary):
                cf_source = normalize_client_address(cf_header)
                if cf_source is not None:
                    _emit_telemetry(ClientSourcePath.CF_CONNECTING_IP)
                    return ClientSourceResolution(cf_source, ClientSourcePath.CF_CONNECTING_IP)
            invalid_forwarding = True

    client = resolve_from_chain_right_to_left(xff_chain, combined_boundary)
    if client is not None:
        _emit_telemetry(
            ClientSourcePath.X_FORWARDED_FOR,
            invalid_forwarding=invalid_forwarding,
        )
        return ClientSourceResolution(client, ClientSourcePath.X_FORWARDED_FOR)

    forwarded_for_values = _parse_forwarded_header_for_values(request)
    if len(forwarded_for_values) > MAX_FORWARDING_CHAIN_LENGTH:
        _emit_telemetry(ClientSourcePath.MALFORMED, invalid_forwarding=True)
        return ClientSourceResolution("unknown", ClientSourcePath.MALFORMED)

    client = resolve_from_chain_right_to_left(forwarded_for_values, combined_boundary)
    if client is not None:
        _emit_telemetry(
            ClientSourcePath.FORWARDED_HEADER,
            invalid_forwarding=invalid_forwarding,
        )
        return ClientSourceResolution(client, ClientSourcePath.FORWARDED_HEADER)

    if _has_forwarding_headers(request):
        _emit_telemetry(ClientSourcePath.MALFORMED, invalid_forwarding=True)
        return ClientSourceResolution("unknown", ClientSourcePath.MALFORMED)

    _emit_telemetry(ClientSourcePath.UNKNOWN)
    return ClientSourceResolution("unknown", ClientSourcePath.UNKNOWN)
