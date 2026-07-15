"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Public Cloudflare edge ranges (https://www.cloudflare.com/ips-v4 / ips-v6).
# Used only to validate that a request chain passed through Cloudflare before
# accepting CF-Connecting-IP or stripping X-Forwarded-For hops.
_CLOUDFLARE_CIDRS = """
173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18
108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17
162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22
2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32
2a06:98c0::/29 2c0f:f248::/32
"""

MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_ADDRESS_LENGTH = 128
FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?:\"?\[?([^;\]\"]+)\]?\"?)(?:;|$)",
    re.IGNORECASE,
)

_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_invalid_forwarding_telemetry_count = 0
_INVALID_TELEMETRY_SAMPLE_RATE = 100


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the non-sensitive path label."""

    source: str
    path: str


def reset_client_source_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _invalid_forwarding_telemetry_count
    with _telemetry_lock:
        _invalid_forwarding_telemetry_count = 0


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses; return None for malformed input."""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_ADDRESS_LENGTH:
        return None
    if candidate.startswith('"') and candidate.endswith('"'):
        candidate = candidate[1:-1].strip()
    if not candidate:
        return None

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":"):
            if not remainder[1:].isdigit():
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if not port.isdigit():
            return None
        candidate = host
    else:
        host = candidate

    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def _split_network_tokens(spec: str) -> list[str]:
    return [token for token in re.split(r"[\s,]+", spec.strip()) if token]


@lru_cache(maxsize=8)
def _parse_network_spec(spec: str) -> tuple[ipaddress._BaseNetwork, ...]:
    networks: list[ipaddress._BaseNetwork] = []
    for entry in _split_network_tokens(spec):
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                host = ipaddress.ip_address(entry)
                prefix = 32 if host.version == 4 else 128
                networks.append(ipaddress.ip_network(f"{entry}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress._BaseNetwork, ...]:
    """Configured immediate-peer proxy boundary for production Render."""
    spec = settings.admin_trusted_proxy_cidrs.strip()
    if not spec:
        return ()
    return _parse_network_spec(spec)


def cloudflare_edge_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    return _parse_network_spec(_CLOUDFLARE_CIDRS)


def address_in_networks(
    address: str,
    networks: Iterable[ipaddress._BaseNetwork],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        ip_obj = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(ip_obj in network for network in networks)


def _record_invalid_forwarding_telemetry(reason: str) -> None:
    global _invalid_forwarding_telemetry_count
    with _telemetry_lock:
        _invalid_forwarding_telemetry_count += 1
        sample_index = _invalid_forwarding_telemetry_count
    if sample_index <= 10 or sample_index % _INVALID_TELEMETRY_SAMPLE_RATE == 0:
        _logger.info(
            "Admin client source ignored forwarding data",
            extra={"reason": reason, "sample_index": sample_index},
        )


def _direct_peer_source(request: Request) -> str:
    if request.client is None:
        return "unknown"
    host = request.client.host.strip()
    if not host:
        return "unknown"
    normalized = normalize_client_address(host)
    if normalized is not None:
        return normalized
    return host.lower()


def _split_forwarded_for(header_value: str) -> list[str]:
    if not header_value or len(header_value) > 4096:
        return []
    parts = [part.strip() for part in header_value.split(",")]
    elements = [part for part in parts if part]
    if len(elements) > MAX_FORWARDED_CHAIN_LENGTH:
        _record_invalid_forwarding_telemetry("xff_chain_too_long")
        return []
    return elements


def _chain_contains_cloudflare_hop(chain: list[str]) -> bool:
    cf_networks = cloudflare_edge_networks()
    return any(address_in_networks(hop, cf_networks) for hop in chain)


def _trusted_hop_networks(settings: Settings) -> tuple[ipaddress._BaseNetwork, ...]:
    return trusted_proxy_networks(settings) + cloudflare_edge_networks()


def _resolve_from_xff_chain(
    chain: list[str],
    *,
    settings: Settings,
) -> str | None:
    if not chain:
        return None
    trusted = _trusted_hop_networks(settings)
    index = len(chain) - 1
    while index >= 0 and address_in_networks(chain[index], trusted):
        index -= 1
    if index < 0:
        _record_invalid_forwarding_telemetry("xff_all_trusted")
        return None
    normalized = normalize_client_address(chain[index])
    if normalized is None:
        _record_invalid_forwarding_telemetry("xff_invalid_client")
        return None
    return normalized


def _parse_forwarded_header(header_value: str) -> list[str]:
    if not header_value or len(header_value) > 4096:
        return []
    addresses: list[str] = []
    for entry in header_value.split(","):
        token = entry.strip()
        if not token:
            continue
        match = FORWARDED_FOR_TOKEN.match(token)
        if match is None:
            continue
        addresses.append(match.group(1).strip())
        if len(addresses) >= MAX_FORWARDED_CHAIN_LENGTH:
            _record_invalid_forwarding_telemetry("forwarded_chain_too_long")
            break
    return addresses


def _peer_is_trusted(request: Request, settings: Settings) -> bool:
    if request.client is None:
        return False
    peer = request.client.host.strip()
    if not peer:
        return False
    networks = trusted_proxy_networks(settings)
    if not networks:
        return False
    return address_in_networks(peer, networks)


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login limiter buckets.

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. Header precedence (when the peer is trusted):

    1. ``CF-Connecting-IP`` when the ``X-Forwarded-For`` chain contains a
       Cloudflare edge hop (proves the request transited Cloudflare).
    2. ``X-Forwarded-For`` parsed right-to-left, skipping trusted proxy hops.
    3. RFC 7239 ``Forwarded`` ``for=`` values with the same trusted-hop walk.
    4. Normalized direct peer address.

    Untrusted peers always use the direct transport address; spoofed headers are
    ignored without affecting login error disclosure.
    """
    peer_source = _direct_peer_source(request)
    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(source=peer_source, path="direct_peer")

    if not _peer_is_trusted(request, settings):
        if _forwarding_headers_present(request):
            _record_invalid_forwarding_telemetry("untrusted_peer")
        return ClientSourceResolution(source=peer_source, path="untrusted_peer")

    xff_chain = _split_forwarded_for(request.headers.get("x-forwarded-for", ""))
    cf_header = request.headers.get("cf-connecting-ip", "").strip()
    cf_normalized = normalize_client_address(cf_header) if cf_header else None

    if cf_normalized and xff_chain and _chain_contains_cloudflare_hop(xff_chain):
        return ClientSourceResolution(source=cf_normalized, path="cf_connecting_ip")

    xff_client = _resolve_from_xff_chain(xff_chain, settings=settings)
    if xff_client is not None:
        return ClientSourceResolution(source=xff_client, path="xff_trusted_chain")

    forwarded_chain = _parse_forwarded_header(request.headers.get("forwarded", ""))
    forwarded_client = _resolve_from_xff_chain(forwarded_chain, settings=settings)
    if forwarded_client is not None:
        return ClientSourceResolution(source=forwarded_client, path="forwarded_header")

    if _forwarding_headers_present(request):
        _record_invalid_forwarding_telemetry("malformed_forwarding")
        return ClientSourceResolution(source=peer_source, path="malformed_forwarding")

    return ClientSourceResolution(source=peer_source, path="trusted_peer")


def _forwarding_headers_present(request: Request) -> bool:
    return any(
        request.headers.get(name, "").strip()
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def admin_proxy_trust_health(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification payload for /health."""
    trusted = trusted_proxy_networks(settings)
    return {
        "proxy_headers_enabled": settings.admin_trust_proxy_headers,
        "trusted_proxy_configured": bool(trusted),
        "trusted_proxy_network_count": len(trusted),
        "uvicorn_forwarded_allow_ips": settings.uvicorn_forwarded_allow_ips.strip(),
    }
