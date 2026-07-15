"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import hashlib
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

_MAX_FORWARD_CHAIN_LENGTH = 32
_INVALID_TELEMETRY_WINDOW_SECONDS = 60
_INVALID_TELEMETRY_MAX_SAMPLES = 10
_UNKNOWN_SOURCE = "unknown"

# Sampled invalid/untrusted forwarding telemetry keyed by reason code.
_invalid_telemetry: dict[str, tuple[int, float]] = {}

_FORWARDED_FOR_RE = re.compile(
    r"""for=(?:"\[([^\]]+)\](?::\d+)?"|([^;,\s"]+))""",
    re.IGNORECASE,
)


class ClientSourcePath(str, Enum):
    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    FORWARDED_HEADER = "forwarded_header"
    UNKNOWN = "unknown"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the resolution path used."""

    source: str
    path: ClientSourcePath


def parse_trusted_networks(
    cidrs: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def normalize_ip(raw: str) -> str | None:
    """Normalize IPv4/IPv6, strip ports, and map IPv4-mapped IPv6."""
    candidate = raw.strip()
    if not candidate:
        return None
    bracketed = candidate.startswith("[") and "]" in candidate
    if bracketed:
        closing = candidate.index("]")
        host_part = candidate[1:closing]
        rest = candidate[closing + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            candidate = host_part
        else:
            candidate = host_part
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed.lower()


def is_trusted_proxy(
    ip_str: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip(ip_str)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks)


def parse_forwarded_for_chain(header: str) -> list[str]:
    return [part.strip() for part in header.split(",") if part.strip()]


def parse_forwarded_header(header: str) -> list[str]:
    values: list[str] = []
    for segment in header.split(","):
        match = _FORWARDED_FOR_RE.search(segment)
        if match is None:
            continue
        raw = match.group(1) or match.group(2) or ""
        if raw:
            values.append(raw.strip())
    return values


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return normalize_ip(request.client.host)


def _build_hop_chain(xff_header: str | None, peer: str | None) -> list[str]:
    hops = parse_forwarded_for_chain(xff_header) if xff_header else []
    if peer and (not hops or hops[-1] != peer):
        hops.append(peer)
    return hops


def _strip_trailing_trusted_hops(
    hops: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> list[str]:
    remaining = list(hops)
    while remaining:
        normalized = normalize_ip(remaining[-1])
        if normalized is None or not is_trusted_proxy(normalized, trusted_networks):
            break
        remaining.pop()
    return remaining


def _resolve_single_hop_chain(
    hops: list[str],
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if len(hops) > _MAX_FORWARD_CHAIN_LENGTH:
        return None
    remaining = _strip_trailing_trusted_hops(hops, trusted_networks)
    if len(remaining) == 1:
        return normalize_ip(remaining[0])
    return None


def _cloudflare_edge_present(
    hops: list[str],
    *,
    render_trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_trusted:
        return False
    remaining = _strip_trailing_trusted_hops(hops, render_trusted)
    if not remaining:
        return False
    rightmost = normalize_ip(remaining[-1])
    if rightmost is None:
        return False
    return is_trusted_proxy(rightmost, cloudflare_trusted)


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _record_invalid_telemetry(reason: str) -> None:
    now = time.monotonic()
    count, window_start = _invalid_telemetry.get(reason, (0, now))
    if now - window_start >= _INVALID_TELEMETRY_WINDOW_SECONDS:
        count = 0
        window_start = now
    count += 1
    _invalid_telemetry[reason] = (count, window_start)
    if count <= _INVALID_TELEMETRY_MAX_SAMPLES:
        _logger.warning(
            "Admin login forwarding headers rejected",
            extra={"reason": reason, "sample_index": count},
        )


def _log_resolution(resolution: ClientSourceResolution) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={
            "resolution_path": resolution.path.value,
            "source_digest": _source_digest(resolution.source),
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer = _peer_host(request)
    render_trusted = parse_trusted_networks(settings.admin_trusted_proxy_cidrs)
    cloudflare_trusted = parse_trusted_networks(settings.admin_cloudflare_proxy_cidrs)

    if not render_trusted or peer is None:
        if peer is None:
            return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=ClientSourcePath.UNKNOWN)
        return ClientSourceResolution(source=peer, path=ClientSourcePath.DIRECT_PEER)

    if not is_trusted_proxy(peer, render_trusted):
        return ClientSourceResolution(source=peer, path=ClientSourcePath.DIRECT_PEER)

    xff_header = request.headers.get("x-forwarded-for")
    hops = _build_hop_chain(xff_header, peer)
    cf_header = request.headers.get("cf-connecting-ip")
    cf_ip = normalize_ip(cf_header) if cf_header else None
    cf_edge = _cloudflare_edge_present(
        hops,
        render_trusted=render_trusted,
        cloudflare_trusted=cloudflare_trusted,
    )

    if cf_ip is not None:
        if cf_edge:
            return ClientSourceResolution(
                source=cf_ip,
                path=ClientSourcePath.CF_CONNECTING_IP,
            )
        _record_invalid_telemetry("cf_connecting_ip_without_cloudflare_hop")

    if xff_header:
        resolved = _resolve_single_hop_chain(hops, render_trusted)
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourcePath.TRUSTED_XFF_CHAIN,
            )
        if len(hops) > _MAX_FORWARD_CHAIN_LENGTH:
            _record_invalid_telemetry("forward_chain_overlong")
        else:
            _record_invalid_telemetry("forward_chain_ambiguous")

    forwarded_header = request.headers.get("forwarded")
    if forwarded_header:
        forwarded_hops = parse_forwarded_header(forwarded_header)
        if peer and (not forwarded_hops or forwarded_hops[-1] != peer):
            forwarded_hops.append(peer)
        resolved = _resolve_single_hop_chain(
            forwarded_hops,
            render_trusted,
        )
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=ClientSourcePath.FORWARDED_HEADER,
            )
        _record_invalid_telemetry("forwarded_header_ambiguous")

    if peer is not None:
        return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=ClientSourcePath.MALFORMED)
    return ClientSourceResolution(source=_UNKNOWN_SOURCE, path=ClientSourcePath.UNKNOWN)


def client_source_for_limiter(request: Request, settings: Settings) -> str:
    """Return normalized source material for admin login limiter keys."""
    resolution = resolve_admin_login_client_source(request, settings)
    _log_resolution(resolution)
    return resolution.source
