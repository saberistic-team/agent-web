"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative upper bound on comma-separated forwarding hops.
_MAX_FORWARDING_CHAIN_LENGTH = 32

# Sample at most one telemetry event per path per interval (no raw addresses).
_TELEMETRY_INTERVAL_SECONDS = 60.0
_telemetry_lock = Lock()
_telemetry_last_emit: dict[str, float] = {}

# Production request chain (documented in docs/ADMIN_AUTH.md):
#   Client → Cloudflare (appends connecting address to X-Forwarded-For)
#   → Render load balancer (immediate peer at Uvicorn; private/loopback)
#   → Uvicorn (--proxy-headers with explicit --forwarded-allow-ips)
#
# Resolution never reads the leftmost raw X-Forwarded-For value unless every
# hop to the right has been verified as a configured trusted proxy.

FORWARDED_FOR_HEADER = "x-forwarded-for"
FORWARDED_HEADER = "forwarded"
CF_CONNECTING_IP_HEADER = "cf-connecting-ip"

# Default trusted networks for Render's internal load balancer hop.
DEFAULT_RENDER_TRUSTED_PROXY_NETWORKS = (
    "127.0.0.1/32,"
    "::1/128,"
    "10.0.0.0/8,"
    "172.16.0.0/12,"
    "192.168.0.0/16"
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: str


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Return normalized client source identity for admin login limiter buckets."""
    peer = _peer_host(request)
    if peer is None:
        _maybe_emit_telemetry("missing_peer")
        return ClientSourceResolution("unknown", "missing_peer")

    peer_normalized = normalize_ip_address(peer)
    if peer_normalized is None:
        if _looks_like_ip_literal(peer):
            _maybe_emit_telemetry("malformed_peer")
            return ClientSourceResolution("unknown", "malformed_peer")
        peer_normalized = peer.strip().lower()

    trusted_networks = _trusted_networks(settings)
    cloudflare_networks = _cloudflare_networks(settings)

    if trusted_networks and _ip_in_networks(peer_normalized, trusted_networks):
        xff_chain = _parse_x_forwarded_for(request.headers.get(FORWARDED_FOR_HEADER, ""))
        if xff_chain is not None:
            resolved, path = _resolve_from_hop_chain(
                peer=peer_normalized,
                hops=xff_chain,
                trusted_networks=trusted_networks,
                path_prefix="xff",
            )
            if resolved is not None:
                _maybe_emit_telemetry(path)
                return ClientSourceResolution(resolved, path)
            _maybe_emit_telemetry("xff_rejected")

        forwarded_chain = _parse_forwarded_header(request.headers.get(FORWARDED_HEADER, ""))
        if forwarded_chain is not None:
            resolved, path = _resolve_from_hop_chain(
                peer=peer_normalized,
                hops=forwarded_chain,
                trusted_networks=trusted_networks,
                path_prefix="forwarded",
            )
            if resolved is not None:
                _maybe_emit_telemetry(path)
                return ClientSourceResolution(resolved, path)
            _maybe_emit_telemetry("forwarded_rejected")

        _maybe_emit_telemetry("trusted_peer_fallback")
        return ClientSourceResolution(peer_normalized, "trusted_peer_fallback")

    cf_ip = request.headers.get(CF_CONNECTING_IP_HEADER, "")
    if (
        cf_ip.strip()
        and cloudflare_networks
        and _ip_in_networks(peer_normalized, cloudflare_networks)
    ):
        candidate = normalize_ip_address(cf_ip)
        if candidate is not None:
            _maybe_emit_telemetry("cf_connecting_ip_verified")
            return ClientSourceResolution(candidate, "cf_connecting_ip_verified")
        _maybe_emit_telemetry("vendor_header_rejected")

    if _has_untrusted_forwarding_headers(request):
        _maybe_emit_telemetry("untrusted_forwarding_rejected")
    return ClientSourceResolution(peer_normalized, "direct_peer")


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 deterministically."""
    candidate = raw.strip()
    if not candidate:
        return None

    candidate = _strip_address_port(candidate)
    if not candidate:
        return None

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.compressed.lower()
    return str(parsed)


def _looks_like_ip_literal(raw: str) -> bool:
    text = raw.strip()
    if not text:
        return False
    if text.startswith("[") and "]" in text:
        return True
    return bool(re.search(r"[.:]", text))


def parse_trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated IPs/CIDR blocks; invalid entries are ignored."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in spec.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            if "/" not in entry:
                addr = ipaddress.ip_address(entry)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
            else:
                networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def deployment_proxy_trust_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification metadata (no raw addresses)."""
    trusted = _trusted_networks(settings)
    return {
        "mode": "trusted_proxy_networks",
        "trusted_networks_configured": bool(trusted),
        "trusted_network_count": len(trusted),
        "legacy_admin_trust_proxy_headers": settings.admin_trust_proxy_headers,
        "uvicorn_proxy_headers": "configured_in_start_command",
        "resolution": "trusted_hop_parser",
    }


def _trusted_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    spec = (settings.admin_trusted_proxy_networks or "").strip()
    if spec:
        return parse_trusted_networks(spec)
    if settings.admin_trust_proxy_headers:
        # Legacy flag alone no longer enables header trust without an explicit boundary.
        return ()
    return ()


def _cloudflare_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    spec = (settings.admin_cloudflare_proxy_networks or "").strip()
    if not spec:
        return ()
    return parse_trusted_networks(spec)


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = (request.client.host or "").strip()
    return host or None


def _strip_address_port(value: str) -> str:
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")].strip()
    if value.count(":") == 1 and "." in value:
        host, _sep, port = value.partition(":")
        if port.isdigit():
            return host.strip()
    return value


def _parse_x_forwarded_for(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    hops: list[str] = []
    for part in raw.split(","):
        hop = part.strip()
        if not hop:
            continue
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        hops.append(normalized)
    if not hops:
        return None
    if len(hops) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return hops


_FORWARDED_PAIR_RE = re.compile(
    r"for=(?:\"([^\"]+)\"|([^;,\s]+))",
    re.IGNORECASE,
)


def _parse_forwarded_header(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    hops: list[str] = []
    for match in _FORWARDED_PAIR_RE.finditer(raw):
        candidate = (match.group(1) or match.group(2) or "").strip()
        if not candidate or candidate.lower() == "unknown":
            continue
        if candidate.startswith("_"):
            continue
        normalized = normalize_ip_address(candidate)
        if normalized is None:
            return None
        hops.append(normalized)
    if not hops:
        return None
    if len(hops) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return hops


def _resolve_from_hop_chain(
    *,
    peer: str,
    hops: list[str],
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
    path_prefix: str,
) -> tuple[str | None, str]:
    """Walk a forwarding chain from the trusted immediate peer inward."""
    if not hops:
        return None, f"{path_prefix}_empty"

    if len(hops) == 1:
        return None, f"{path_prefix}_single_hop_unverified"

    chain = list(hops)
    if chain[-1] != peer:
        chain.append(peer)

    while chain and (chain[-1] == peer or _ip_in_networks(chain[-1], trusted_networks)):
        chain.pop()

    if not chain:
        return None, f"{path_prefix}_all_trusted"

    if len(hops) == 2:
        # Cloudflare append semantics: two-hop chains use the rightmost address.
        candidate = chain[-1]
        if candidate == peer or _ip_in_networks(candidate, trusted_networks):
            return None, f"{path_prefix}_invalid_client"
        return candidate, f"{path_prefix}_trusted_chain"

    # Longer chains: leftmost address after removing verified trusted tail hops.
    candidate = chain[0]
    if candidate == peer or _ip_in_networks(candidate, trusted_networks):
        return None, f"{path_prefix}_invalid_client"
    return candidate, f"{path_prefix}_trusted_chain"


def _ip_in_networks(
    ip_str: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def _has_untrusted_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in (
            FORWARDED_FOR_HEADER,
            FORWARDED_HEADER,
            CF_CONNECTING_IP_HEADER,
        )
    )


def _maybe_emit_telemetry(path: str) -> None:
    now = time.monotonic()
    with _telemetry_lock:
        last = _telemetry_last_emit.get(path, 0.0)
        if now - last < _TELEMETRY_INTERVAL_SECONDS:
            return
        _telemetry_last_emit[path] = now
    _logger.info(
        "Admin login client source resolution",
        extra={"resolution_path": path},
    )


def reset_client_source_telemetry() -> None:
    """Clear telemetry sampling state (tests only)."""
    with _telemetry_lock:
        _telemetry_last_emit.clear()
