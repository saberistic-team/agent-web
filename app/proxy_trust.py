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

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_UNKNOWN_SOURCE = "unknown"

# Telemetry sampling for invalid/untrusted forwarding attempts (no raw IPs logged).
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_invalid_forwarding_lock = threading.Lock()
_invalid_forwarding_last_log = 0.0
_invalid_forwarding_suppressed = 0

# Resolution paths surfaced in structured logs (no addresses).
RESOLUTION_DIRECT_PEER = "direct_peer"
RESOLUTION_FORWARDED_CHAIN = "forwarded_chain"
RESOLUTION_CF_CONNECTING_IP = "cf_connecting_ip"
RESOLUTION_UNTRUSTED_PEER = "untrusted_peer"
RESOLUTION_UNTRUSTED_FORWARDING = "untrusted_forwarding"
RESOLUTION_AMBIGUOUS_FORWARDING = "ambiguous_forwarding"
RESOLUTION_MISSING_PEER = "missing_peer"
RESOLUTION_MALFORMED_FORWARDING = "malformed_forwarding"

_FORWARDED_FOR_TOKEN = re.compile(
    r"^\s*(?P<value>[^,;\s]+(?:\:[0-9]+)?)\s*(?:,\s*)?$"
)
_FORWARDED_FOR_ENTRY = re.compile(
    r"for=(?:\"(?P<quoted>[^\"]+)\"|(?P<unquoted>[^;,]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and observability metadata."""

    source: str
    resolution_path: str
    had_forwarding_headers: bool = False
    invalid_forwarding: bool = False


def reset_proxy_trust_telemetry() -> None:
    """Clear rate-limited telemetry counters (tests only)."""
    global _invalid_forwarding_last_log, _invalid_forwarding_suppressed
    with _invalid_forwarding_lock:
        _invalid_forwarding_last_log = 0.0
        _invalid_forwarding_suppressed = 0


def parse_trusted_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        value = raw.strip()
        if not value:
            continue
        networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 for deterministic limiter keys."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if "%" in value:
        value = value.split("%", 1)[0]
    host = value
    if host.count(":") == 1 and "." in host:
        host = host.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def is_trusted_proxy(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _split_forwarded_for(header_value: str) -> list[str]:
    if len(header_value) > 8192:
        return []
    entries: list[str] = []
    for part in header_value.split(","):
        token = part.strip()
        if not token:
            continue
        if not _FORWARDED_FOR_TOKEN.match(token + ","):
            return []
        entries.append(token)
        if len(entries) > MAX_FORWARDING_CHAIN_LENGTH:
            return []
    return entries


def _parse_forwarded_header(header_value: str) -> list[str]:
    if len(header_value) > 8192:
        return []
    entries: list[str] = []
    for segment in header_value.split(","):
        match = _FORWARDED_FOR_ENTRY.search(segment)
        if match is None:
            continue
        raw = match.group("quoted") or match.group("unquoted") or ""
        raw = raw.strip()
        if raw.lower().startswith("unknown"):
            continue
        if raw.startswith("[") and "]" in raw:
            host = raw[1 : raw.index("]")]
        else:
            host = raw.split(":", 1)[0] if raw.count(":") == 1 and "." in raw else raw
        normalized = normalize_ip_address(host)
        if normalized is None:
            return []
        entries.append(normalized)
        if len(entries) > MAX_FORWARDING_CHAIN_LENGTH:
            return []
    return entries


def _collect_forwarding_hops(
    headers: Iterable[tuple[str, str]],
) -> tuple[list[str], bool, bool]:
    """Return hop list, had_any_forwarding_header, malformed."""
    header_map: dict[str, str] = {}
    for name, value in headers:
        key = name.lower()
        if key not in header_map:
            header_map[key] = value

    had_any = False
    malformed = False

    xff_raw = header_map.get("x-forwarded-for", "")
    forwarded_raw = header_map.get("forwarded", "")
    cf_raw = header_map.get("cf-connecting-ip", "")

    if xff_raw or forwarded_raw or cf_raw:
        had_any = True

    xff_hops: list[str] = []
    if xff_raw:
        raw_entries = _split_forwarded_for(xff_raw)
        if not raw_entries and xff_raw.strip():
            malformed = True
        for entry in raw_entries:
            normalized = normalize_ip_address(entry)
            if normalized is None:
                malformed = True
                xff_hops = []
                break
            xff_hops.append(normalized)

    forwarded_hops: list[str] = []
    if forwarded_raw:
        forwarded_hops = _parse_forwarded_header(forwarded_raw)
        if not forwarded_hops and forwarded_raw.strip():
            malformed = True

    # Documented precedence: X-Forwarded-For, then RFC 7239 Forwarded.
    hops = xff_hops or forwarded_hops
    return hops, had_any, malformed


def _cloudflare_hop_present(
    hops: list[str],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not cloudflare_networks:
        return False
    return any(is_trusted_proxy(hop, cloudflare_networks) for hop in hops)


def _is_skippable_hop(
    address: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return is_trusted_proxy(address, trusted_networks) or is_trusted_proxy(
        address, cloudflare_networks
    )


def _resolve_from_trusted_chain(
    *,
    hops: list[str],
    immediate_peer: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cf_connecting_ip: str | None,
) -> ClientSourceResolution:
    chain = list(hops)
    peer_normalized = normalize_ip_address(immediate_peer)
    if peer_normalized and (not chain or chain[-1] != peer_normalized):
        chain.append(peer_normalized)

    if cf_connecting_ip and _cloudflare_hop_present(chain, cloudflare_networks):
        cf_normalized = normalize_ip_address(cf_connecting_ip)
        if cf_normalized is not None:
            return ClientSourceResolution(
                source=cf_normalized,
                resolution_path=RESOLUTION_CF_CONNECTING_IP,
                had_forwarding_headers=True,
            )

    index = len(chain) - 1
    while index >= 0 and _is_skippable_hop(
        chain[index], trusted_networks, cloudflare_networks
    ):
        index -= 1

    if index < 0:
        return ClientSourceResolution(
            source=chain[0],
            resolution_path=RESOLUTION_FORWARDED_CHAIN,
            had_forwarding_headers=True,
        )

    if len(hops) == 1 and index == 0 and peer_normalized is not None:
        # Single XFF hop with a trusted immediate peer is ambiguous (prepend spoof).
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            resolution_path=RESOLUTION_AMBIGUOUS_FORWARDING,
            had_forwarding_headers=True,
            invalid_forwarding=True,
        )

    return ClientSourceResolution(
        source=chain[index],
        resolution_path=RESOLUTION_FORWARDED_CHAIN,
        had_forwarding_headers=True,
    )


def resolve_admin_login_client_source(request: Request, settings: Settings) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    immediate_peer = request.client.host if request.client is not None else None
    header_items = list(request.headers.items())

    if immediate_peer is None:
        _emit_resolution_telemetry(
            ClientSourceResolution(
                source=_UNKNOWN_SOURCE,
                resolution_path=RESOLUTION_MISSING_PEER,
            )
        )
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            resolution_path=RESOLUTION_MISSING_PEER,
        )

    peer_normalized = normalize_ip_address(immediate_peer)
    if peer_normalized is None:
        fallback = immediate_peer.strip().lower()
        peer_normalized = fallback or None
    if peer_normalized is None:
        _emit_resolution_telemetry(
            ClientSourceResolution(
                source=_UNKNOWN_SOURCE,
                resolution_path=RESOLUTION_MALFORMED_FORWARDING,
                invalid_forwarding=True,
            )
        )
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            resolution_path=RESOLUTION_MALFORMED_FORWARDING,
            invalid_forwarding=True,
        )

    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(
            source=peer_normalized,
            resolution_path=RESOLUTION_DIRECT_PEER,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    trusted_networks = parse_trusted_networks(settings.admin_trusted_proxy_cidrs)
    cloudflare_networks = parse_trusted_networks(settings.admin_cloudflare_proxy_cidrs)

    hops, had_forwarding, malformed = _collect_forwarding_hops(header_items)
    cf_header = next(
        (value for name, value in header_items if name.lower() == "cf-connecting-ip"),
        "",
    )

    if malformed:
        resolution = ClientSourceResolution(
            source=peer_normalized,
            resolution_path=RESOLUTION_MALFORMED_FORWARDING,
            had_forwarding_headers=had_forwarding,
            invalid_forwarding=True,
        )
        _emit_invalid_forwarding(resolution.resolution_path)
        _emit_resolution_telemetry(resolution)
        return resolution

    if not is_trusted_proxy(peer_normalized, trusted_networks):
        if had_forwarding:
            _emit_invalid_forwarding(RESOLUTION_UNTRUSTED_FORWARDING)
        resolution = ClientSourceResolution(
            source=peer_normalized,
            resolution_path=(
                RESOLUTION_UNTRUSTED_FORWARDING
                if had_forwarding
                else RESOLUTION_UNTRUSTED_PEER
            ),
            had_forwarding_headers=had_forwarding,
            invalid_forwarding=had_forwarding,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    if not hops and not cf_header:
        resolution = ClientSourceResolution(
            source=peer_normalized,
            resolution_path=RESOLUTION_DIRECT_PEER,
        )
        _emit_resolution_telemetry(resolution)
        return resolution

    cf_connecting_ip = cf_header.strip() or None
    if cf_connecting_ip and not _cloudflare_hop_present(hops, cloudflare_networks):
        cf_connecting_ip = None
        if cf_header.strip():
            _emit_invalid_forwarding(RESOLUTION_UNTRUSTED_FORWARDING)

    resolution = _resolve_from_trusted_chain(
        hops=hops,
        immediate_peer=peer_normalized,
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
        cf_connecting_ip=cf_connecting_ip,
    )
    if resolution.invalid_forwarding:
        _emit_invalid_forwarding(resolution.resolution_path)
    _emit_resolution_telemetry(resolution)
    return resolution


def _emit_invalid_forwarding(reason: str) -> None:
    global _invalid_forwarding_last_log, _invalid_forwarding_suppressed
    now = time.monotonic()
    with _invalid_forwarding_lock:
        elapsed = now - _invalid_forwarding_last_log
        if elapsed < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
            _invalid_forwarding_suppressed += 1
            return
        suppressed = _invalid_forwarding_suppressed
        _invalid_forwarding_suppressed = 0
        _invalid_forwarding_last_log = now
    _logger.warning(
        "Admin login forwarding headers ignored",
        extra={
            "reason": reason,
            "suppressed_since_last": suppressed,
        },
    )


def _emit_resolution_telemetry(resolution: ClientSourceResolution) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={
            "resolution_path": resolution.resolution_path,
            "had_forwarding_headers": resolution.had_forwarding_headers,
            "invalid_forwarding": resolution.invalid_forwarding,
        },
    )
