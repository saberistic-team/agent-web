"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import threading
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from app.config import Settings

SourceResolutionPath = Literal[
    "direct_peer",
    "xff_trusted_chain",
    "cf_connecting_ip",
    "forwarded_header",
    "untrusted_forwarded_rejected",
    "malformed_forwarding",
    "missing_peer",
]

MAX_FORWARDING_CHAIN_LENGTH = 32
_TELEMETRY_SAMPLE_RATE = 100
_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_telemetry_counter = 0
_invalid_forwarding_counter = 0

# Render internal proxy + loopback (see docs/ADMIN_AUTH.md).
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without retaining raw forwarding headers."""

    source: str
    path: SourceResolutionPath
    trusted_peer: bool


def reset_source_resolution_telemetry() -> None:
    """Clear telemetry counters (tests only)."""
    global _telemetry_counter, _invalid_forwarding_counter
    with _telemetry_lock:
        _telemetry_counter = 0
        _invalid_forwarding_counter = 0


def client_source_trust_mode(settings: Settings) -> str:
    """Return deployment trust mode for health checks (no raw CIDR values)."""
    if settings.admin_trusted_proxy_cidrs:
        return "configured"
    return "direct_only"


def parse_cidr_list(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDR tokens; invalid entries are skipped."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def effective_trusted_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    """Return configured trusted-proxy CIDRs, with legacy boolean fallback."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    if settings.admin_trust_proxy_headers:
        return DEFAULT_TRUSTED_PROXY_CIDRS
    return ()


def effective_trusted_proxy_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_cidr_list(",".join(effective_trusted_proxy_cidrs(settings)))


def effective_forwarding_skip_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks skipped when walking forwarding chains right-to-left."""
    combined = ",".join(
        [*effective_trusted_proxy_cidrs(settings), *settings.admin_cloudflare_edge_cidrs]
    )
    return parse_cidr_list(combined)


def effective_cloudflare_edge_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_cidr_list(",".join(settings.admin_cloudflare_edge_cidrs))


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 for deterministic limiter keys."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and "." in value:
        value = value.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def is_trusted_proxy_address(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(ip in network for network in trusted_networks)


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    if not host:
        return None
    normalized = normalize_client_address(host)
    return normalized if normalized is not None else host


def _split_forwarding_chain(header_value: str) -> list[str]:
    if len(header_value) > 2048:
        return []
    parts = [segment.strip() for segment in header_value.split(",")]
    return [segment for segment in parts if segment]


def _resolve_from_x_forwarded_for(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, SourceResolutionPath | None]:
    chain = _split_forwarding_chain(header_value)
    if not chain:
        return None, "malformed_forwarding"
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None, "malformed_forwarding"

    normalized_hops: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None, "malformed_forwarding"
        normalized_hops.append(normalized)

    for hop in reversed(normalized_hops):
        if is_trusted_proxy_address(hop, trusted_networks):
            continue
        return hop, "xff_trusted_chain"
    return None, None


def _cloudflare_hop_verified(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    chain = _split_forwarding_chain(header_value)
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        if is_trusted_proxy_address(normalized, cloudflare_networks):
            return True
        if is_trusted_proxy_address(normalized, trusted_networks):
            continue
    return False


def _parse_forwarded_for_values(header_value: str) -> list[str]:
    values: list[str] = []
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        for part in entry.split(";"):
            token = part.strip()
            if not token.lower().startswith("for="):
                continue
            raw = token[4:].strip().strip('"')
            if raw.lower() in ("unknown", "-"):
                continue
            values.append(raw)
    return values


def _resolve_from_forwarded_header(
    header_value: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str | None, SourceResolutionPath | None]:
    candidates = _parse_forwarded_for_values(header_value)
    if not candidates:
        return None, "malformed_forwarding"
    if len(candidates) > MAX_FORWARDING_CHAIN_LENGTH:
        return None, "malformed_forwarding"

    normalized_hops: list[str] = []
    for candidate in candidates:
        normalized = normalize_client_address(candidate)
        if normalized is None:
            return None, "malformed_forwarding"
        normalized_hops.append(normalized)

    for hop in reversed(normalized_hops):
        if is_trusted_proxy_address(hop, trusted_networks):
            continue
        return hop, "forwarded_header"
    return None, None


def _record_source_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    global _telemetry_counter, _invalid_forwarding_counter
    with _telemetry_lock:
        _telemetry_counter += 1
        if resolution.path in ("untrusted_forwarded_rejected", "malformed_forwarding"):
            _invalid_forwarding_counter += 1
            should_log = _invalid_forwarding_counter == 1 or (
                _invalid_forwarding_counter % _TELEMETRY_SAMPLE_RATE == 0
            )
        else:
            should_log = _telemetry_counter == 1 or (_telemetry_counter % _TELEMETRY_SAMPLE_RATE == 0)

    if not should_log:
        return

    _logger.info(
        "Admin login client source resolved",
        extra={
            "source_resolution_path": resolution.path,
            "trusted_peer": resolution.trusted_peer,
            "invalid_forwarding_sampled": resolution.path
            in ("untrusted_forwarded_rejected", "malformed_forwarding"),
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
    *,
    emit_telemetry: bool = True,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    trusted_networks = effective_trusted_proxy_networks(settings)
    skip_networks = effective_forwarding_skip_networks(settings)
    cloudflare_networks = effective_cloudflare_edge_networks(settings)
    peer = _immediate_peer_host(request)

    if peer is None:
        resolution = ClientSourceResolution(
            source="unknown",
            path="missing_peer",
            trusted_peer=False,
        )
        if emit_telemetry:
            _record_source_resolution_telemetry(resolution)
        return resolution

    peer_is_trusted = bool(trusted_networks) and is_trusted_proxy_address(
        peer, trusted_networks
    )
    if not peer_is_trusted:
        path: SourceResolutionPath = (
            "untrusted_forwarded_rejected"
            if _has_forwarding_headers(request)
            else "direct_peer"
        )
        resolution = ClientSourceResolution(
            source=peer,
            path=path,
            trusted_peer=False,
        )
        if emit_telemetry:
            _record_source_resolution_telemetry(resolution)
        return resolution

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        client, path = _resolve_from_x_forwarded_for(
            xff,
            trusted_networks=skip_networks,
        )
        if client is not None and path is not None:
            resolution = ClientSourceResolution(
                source=client,
                path=path,
                trusted_peer=True,
            )
            if emit_telemetry:
                _record_source_resolution_telemetry(resolution)
            return resolution
        if path == "malformed_forwarding":
            resolution = ClientSourceResolution(
                source=peer,
                path="malformed_forwarding",
                trusted_peer=True,
            )
            if emit_telemetry:
                _record_source_resolution_telemetry(resolution)
            return resolution

    cf_connecting_ip = request.headers.get("cf-connecting-ip", "")
    if cf_connecting_ip and xff and _cloudflare_hop_verified(
        xff,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
    ):
        normalized = normalize_client_address(cf_connecting_ip)
        if normalized is not None:
            resolution = ClientSourceResolution(
                source=normalized,
                path="cf_connecting_ip",
                trusted_peer=True,
            )
            if emit_telemetry:
                _record_source_resolution_telemetry(resolution)
            return resolution

    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        client, path = _resolve_from_forwarded_header(
            forwarded,
            trusted_networks=skip_networks,
        )
        if client is not None and path is not None:
            resolution = ClientSourceResolution(
                source=client,
                path=path,
                trusted_peer=True,
            )
            if emit_telemetry:
                _record_source_resolution_telemetry(resolution)
            return resolution
        if path == "malformed_forwarding":
            resolution = ClientSourceResolution(
                source=peer,
                path="malformed_forwarding",
                trusted_peer=True,
            )
            if emit_telemetry:
                _record_source_resolution_telemetry(resolution)
            return resolution

    if _has_forwarding_headers(request):
        resolution = ClientSourceResolution(
            source=peer,
            path="untrusted_forwarded_rejected",
            trusted_peer=True,
        )
    else:
        resolution = ClientSourceResolution(
            source=peer,
            path="direct_peer",
            trusted_peer=True,
        )
    if emit_telemetry:
        _record_source_resolution_telemetry(resolution)
    return resolution


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )
