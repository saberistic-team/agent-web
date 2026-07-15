"""Trusted-proxy client source resolution for rate limiting and telemetry."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH = 10
MAX_FORWARDED_HEADER_LENGTH = 1024
_UNKNOWN_SOURCE = "unknown"
_INVALID_TELEMETRY_INTERVAL_SECONDS = 60.0

# RFC 7239 Forwarded: for="203.0.113.1" or for=[2001:db8::1]:8080
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"?\[([^\]]+)\]"?|"?(?<!\[)([^;,\s"]+)"?)',
    re.IGNORECASE,
)


class SourceResolutionPath(StrEnum):
    """Bounded telemetry identifiers; never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_CHAIN_XFF = "trusted_chain_xff"
    CLOUDFLARE_CONNECTING_IP = "cloudflare_connecting_ip"
    TRUSTED_CHAIN_FORWARDED = "trusted_chain_forwarded"
    UNTRUSTED_FORWARDING_IGNORED = "untrusted_forwarding_ignored"
    MALFORMED_FORWARDING = "malformed_forwarding"
    MISSING_PEER = "missing_peer"
    ALL_TRUSTED_CHAIN = "all_trusted_chain"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: SourceResolutionPath


_telemetry_lock = threading.Lock()
_telemetry_last_invalid_log_at = 0.0


def _peer_identity(raw: str | None) -> str | None:
    """Normalize a TCP peer to a limiter source string."""
    if raw is None:
        return None
    normalized = normalize_ip_address(raw)
    if normalized is not None:
        return normalized
    stripped = raw.strip().lower()
    return stripped or None


def parse_trusted_cidrs(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDR literals; ignore empty or invalid entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 for deterministic limiter keys; return None when invalid."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.lower() == _UNKNOWN_SOURCE:
        return _UNKNOWN_SOURCE

    # Strip bracketed IPv6 and trailing :port for IPv4 host:port forms.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _port = candidate.rsplit(":", 1)
        if _port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed
    return str(parsed)


def is_trusted_proxy(
    ip: str | None,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip_address(ip)
    if normalized is None or normalized == _UNKNOWN_SOURCE:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks)


def _split_forwarded_chain(raw: str) -> list[str] | None:
    if len(raw) > MAX_FORWARDED_HEADER_LENGTH:
        return None
    hops = [hop.strip() for hop in raw.split(",") if hop.strip()]
    if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_hops.append(normalized)
    return normalized_hops


def _walk_trusted_chain(
    chain: Sequence[str],
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    for hop in reversed(chain):
        if not is_trusted_proxy(hop, trusted_networks):
            return hop
    return None


def _parse_forwarded_header(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    if len(raw) > MAX_FORWARDED_HEADER_LENGTH:
        return None
    hops: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(raw):
        candidate = match.group(1) or match.group(2)
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return None
        hops.append(normalized)
        if len(hops) > MAX_FORWARDED_CHAIN_LENGTH:
            return None
    return hops or None


def _cloudflare_hop_present(
    chain: Sequence[str],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(is_trusted_proxy(hop, cloudflare_networks) for hop in chain)


def _effective_trusted_hops(
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    trust_cloudflare_edge: bool,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if trust_cloudflare_edge and cloudflare_networks:
        return (*trusted_networks, *cloudflare_networks)
    return tuple(trusted_networks)


def resolve_client_source(
    *,
    immediate_peer: str | None,
    x_forwarded_for: str | None,
    forwarded: str | None,
    cf_connecting_ip: str | None,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    trust_cloudflare_edge: bool,
) -> ClientSourceResolution:
    """Resolve the effective client source using a right-to-left trusted-hop model.

    Precedence (documented in ``docs/ADMIN_AUTH.md``):

    1. Untrusted immediate peer — use the peer address; ignore forwarding headers.
    2. Trusted immediate peer with validated ``CF-Connecting-IP`` after a Cloudflare
       hop is observed in ``X-Forwarded-For``.
    3. Trusted immediate peer — walk ``X-Forwarded-For`` from right to left across
       configured trusted proxy CIDRs.
    4. Trusted immediate peer — parse ``Forwarded`` when ``X-Forwarded-For`` is absent.
    5. Malformed or overlong forwarding data — conservative fallback to the peer.
    """
    normalized_peer = _peer_identity(immediate_peer)
    if normalized_peer is None:
        return ClientSourceResolution(
            source=_UNKNOWN_SOURCE,
            path=SourceResolutionPath.MISSING_PEER,
        )

    hop_networks = _effective_trusted_hops(
        trusted_networks,
        cloudflare_networks,
        trust_cloudflare_edge,
    )

    if not is_trusted_proxy(normalized_peer, trusted_networks):
        if x_forwarded_for or forwarded or cf_connecting_ip:
            return ClientSourceResolution(
                source=normalized_peer,
                path=SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED,
            )
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.DIRECT_PEER,
        )

    xff_chain = _split_forwarded_chain(x_forwarded_for) if x_forwarded_for else None
    if x_forwarded_for and xff_chain is None:
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.MALFORMED_FORWARDING,
        )

    if trust_cloudflare_edge and cf_connecting_ip:
        normalized_cf = normalize_ip_address(cf_connecting_ip)
        if normalized_cf is None:
            return ClientSourceResolution(
                source=normalized_peer,
                path=SourceResolutionPath.MALFORMED_FORWARDING,
            )
        cf_chain = xff_chain or []
        if cloudflare_networks and _cloudflare_hop_present(cf_chain, cloudflare_networks):
            return ClientSourceResolution(
                source=normalized_cf,
                path=SourceResolutionPath.CLOUDFLARE_CONNECTING_IP,
            )

    if xff_chain is not None:
        full_chain = [*xff_chain, normalized_peer]
        client = _walk_trusted_chain(full_chain, hop_networks)
        if client is None:
            return ClientSourceResolution(
                source=normalized_peer,
                path=SourceResolutionPath.ALL_TRUSTED_CHAIN,
            )
        return ClientSourceResolution(
            source=client,
            path=SourceResolutionPath.TRUSTED_CHAIN_XFF,
        )

    forwarded_chain = _parse_forwarded_header(forwarded)
    if forwarded and forwarded_chain is None:
        return ClientSourceResolution(
            source=normalized_peer,
            path=SourceResolutionPath.MALFORMED_FORWARDING,
        )
    if forwarded_chain is not None:
        full_chain = [*forwarded_chain, normalized_peer]
        client = _walk_trusted_chain(full_chain, hop_networks)
        if client is None:
            return ClientSourceResolution(
                source=normalized_peer,
                path=SourceResolutionPath.ALL_TRUSTED_CHAIN,
            )
        return ClientSourceResolution(
            source=client,
            path=SourceResolutionPath.TRUSTED_CHAIN_FORWARDED,
        )

    return ClientSourceResolution(
        source=normalized_peer,
        path=SourceResolutionPath.DIRECT_PEER,
    )


def resolve_admin_login_client_source(
    *,
    immediate_peer: str | None,
    headers: dict[str, str],
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    trust_cloudflare_edge: bool,
) -> ClientSourceResolution:
    """Resolve admin login limiter source from request peer and forwarding headers."""
    lowered = {key.lower(): value for key, value in headers.items()}
    return resolve_client_source(
        immediate_peer=immediate_peer,
        x_forwarded_for=lowered.get("x-forwarded-for"),
        forwarded=lowered.get("forwarded"),
        cf_connecting_ip=lowered.get("cf-connecting-ip"),
        trusted_networks=trusted_networks,
        cloudflare_networks=cloudflare_networks,
        trust_cloudflare_edge=trust_cloudflare_edge,
    )


def reset_source_resolution_telemetry_for_tests() -> None:
    """Clear sampled invalid-forwarding telemetry state (tests only)."""
    global _telemetry_last_invalid_log_at
    with _telemetry_lock:
        _telemetry_last_invalid_log_at = 0.0


def log_source_resolution(
    resolution: ClientSourceResolution,
    *,
    context: str = "admin_login",
) -> None:
    """Emit bounded telemetry without raw addresses or header values."""
    extra = {
        "source_resolution_path": resolution.path.value,
        "source_resolution_context": context,
    }
    if resolution.path in {
        SourceResolutionPath.UNTRUSTED_FORWARDING_IGNORED,
        SourceResolutionPath.MALFORMED_FORWARDING,
        SourceResolutionPath.ALL_TRUSTED_CHAIN,
    }:
        _log_invalid_forwarding(extra)
        return
    _logger.debug("Client source resolved", extra=extra)


def _log_invalid_forwarding(extra: dict[str, str]) -> None:
    global _telemetry_last_invalid_log_at
    now = time.monotonic()
    with _telemetry_lock:
        if now - _telemetry_last_invalid_log_at < _INVALID_TELEMETRY_INTERVAL_SECONDS:
            return
        _telemetry_last_invalid_log_at = now
    _logger.info("Ignored or rejected forwarding headers for client source", extra=extra)


def proxy_trust_health_summary(
    *,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    cloudflare_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    trust_cloudflare_edge: bool,
    uvicorn_forwarded_allow_ips: str,
) -> dict[str, object]:
    """Non-sensitive deployment verification payload for ``GET /health``."""
    return {
        "trusted_proxy_configured": bool(trusted_networks),
        "trusted_proxy_network_count": len(trusted_networks),
        "cloudflare_edge_trust_enabled": trust_cloudflare_edge,
        "cloudflare_edge_network_count": len(cloudflare_networks),
        "uvicorn_forwarded_allow_ips": uvicorn_forwarded_allow_ips,
    }
