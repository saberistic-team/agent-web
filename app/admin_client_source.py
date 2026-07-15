"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Render load balancers and loopback peers seen in production.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "127.0.0.1/32",
    "::1/128",
)

# Cloudflare published edge ranges (https://www.cloudflare.com/ips/) used only
# to strip trusted hops and to prove CF-Connecting-IP came through the edge.
CLOUDFLARE_EDGE_CIDRS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

MAX_FORWARDED_CHAIN_LENGTH = 32
MAX_FORWARDED_HEADER_BYTES = 2048
FORWARDED_FOR_TOKEN = re.compile(r"for=(\"([^\"\\]|\\.)*\"|[^;\s,]+)", re.IGNORECASE)

_logger = logging.getLogger(__name__)
_telemetry_lock = threading.Lock()
_telemetry_counters: dict[str, int] = {}
_INVALID_FORWARDING_LOG_EVERY = 64


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and a privacy-safe telemetry path label."""

    source: str
    path: str
    had_forwarding_headers: bool = False


def parse_trusted_proxy_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        candidate = raw.strip()
        if not candidate:
            continue
        if "/" not in candidate:
            address = ipaddress.ip_address(candidate)
            networks.append(
                ipaddress.ip_network(f"{candidate}/{'128' if address.version == 6 else '32'}", strict=False)
            )
            continue
        networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


def configured_trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return configured proxy networks plus Cloudflare edge ranges when trust is enabled."""
    if not settings.admin_proxy_trust_enabled:
        return ()
    cidrs = settings.admin_trusted_proxy_cidrs
    if not cidrs and settings.admin_trust_proxy_headers:
        cidrs = DEFAULT_RENDER_TRUSTED_PROXY_CIDRS
    combined = tuple(dict.fromkeys((*cidrs, *CLOUDFLARE_EDGE_CIDRS)))
    return parse_trusted_proxy_networks(combined)


def cloudflare_edge_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxy_networks(CLOUDFLARE_EDGE_CIDRS)


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; return None when invalid."""
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, _port = candidate.rsplit(":", 1)
        if _port.isdigit():
            candidate = host
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return address.compressed
    return str(address)


def _address_in_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarded_for(header_value: str) -> list[str]:
    if len(header_value) > MAX_FORWARDED_HEADER_BYTES:
        return []
    parts = [part.strip() for part in header_value.split(",") if part.strip()]
    if len(parts) > MAX_FORWARDED_CHAIN_LENGTH:
        return []
    return parts


def _parse_forwarded_header(header_value: str) -> list[str]:
    if len(header_value) > MAX_FORWARDED_HEADER_BYTES:
        return []
    addresses: list[str] = []
    for match in FORWARDED_FOR_TOKEN.finditer(header_value):
        token = match.group(1).strip()
        if token.startswith('"') and token.endswith('"'):
            token = token[1:-1]
        if token.lower() == "unknown":
            continue
        if token.startswith("[") and "]" in token:
            token = token[1 : token.index("]")]
        elif token.count(":") == 1 and "." in token:
            host, port = token.rsplit(":", 1)
            if port.isdigit():
                token = host
        addresses.append(token)
        if len(addresses) > MAX_FORWARDED_CHAIN_LENGTH:
            return []
    return addresses


def _select_client_from_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    normalized_chain: list[str] = []
    for hop in reversed(chain):
        normalized = normalize_ip_address(hop)
        if normalized is None:
            return None
        normalized_chain.append(normalized)
    for hop in normalized_chain:
        if not _address_in_networks(hop, trusted_networks):
            return hop
    return None


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    normalized = normalize_ip_address(request.client.host)
    if normalized is not None:
        return normalized
    raw = request.client.host.strip()
    return raw or None


def _request_has_forwarding_headers(request: Request) -> bool:
    header_names = (
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    )
    return any(request.headers.get(name) for name in header_names)


def record_client_source_telemetry(resolution: ClientSourceResolution) -> None:
    """Emit bounded structured telemetry without raw addresses or header values."""
    with _telemetry_lock:
        _telemetry_counters[resolution.path] = _telemetry_counters.get(resolution.path, 0) + 1
        count = _telemetry_counters[resolution.path]
    extra = {
        "client_source_path": resolution.path,
        "had_forwarding_headers": resolution.had_forwarding_headers,
    }
    if resolution.path == "invalid_forwarding" and count % _INVALID_FORWARDING_LOG_EVERY == 1:
        _logger.info("Admin login client source used invalid forwarding data", extra=extra)
        return
    if count <= 3 or count % 256 == 0:
        _logger.debug("Admin login client source resolved", extra=extra)


def reset_client_source_telemetry_for_tests() -> None:
    with _telemetry_lock:
        _telemetry_counters.clear()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    had_forwarding_headers = _request_has_forwarding_headers(request)
    peer = _peer_host(request)
    if peer is None:
        return ClientSourceResolution(
            source="unknown",
            path="missing_peer",
            had_forwarding_headers=had_forwarding_headers,
        )

    trusted_networks = configured_trusted_proxy_networks(settings)
    if not trusted_networks:
        return ClientSourceResolution(
            source=peer,
            path="direct_peer",
            had_forwarding_headers=had_forwarding_headers,
        )

    if not _address_in_networks(peer, trusted_networks):
        if had_forwarding_headers:
            record_client_source_telemetry(
                ClientSourceResolution(
                    source="unknown",
                    path="invalid_forwarding",
                    had_forwarding_headers=True,
                )
            )
            return ClientSourceResolution(
                source="unknown",
                path="direct_untrusted_peer",
                had_forwarding_headers=True,
            )
        return ClientSourceResolution(
            source=peer,
            path="direct_untrusted_peer",
            had_forwarding_headers=False,
        )

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_chain = _split_forwarded_for(xff_header) if xff_header else []
    if xff_header and not xff_chain:
        return ClientSourceResolution(
            source="unknown",
            path="invalid_forwarding",
            had_forwarding_headers=True,
        )

    if xff_chain:
        selected = _select_client_from_chain(xff_chain, trusted_networks=trusted_networks)
        if selected is None:
            return ClientSourceResolution(
                source="unknown",
                path="invalid_forwarding",
                had_forwarding_headers=True,
            )
        return ClientSourceResolution(
            source=selected,
            path="trusted_x_forwarded_for",
            had_forwarding_headers=True,
        )

    forwarded_header = request.headers.get("forwarded", "")
    forwarded_chain = _parse_forwarded_header(forwarded_header) if forwarded_header else []
    if forwarded_header and not forwarded_chain:
        return ClientSourceResolution(
            source="unknown",
            path="invalid_forwarding",
            had_forwarding_headers=True,
        )
    if forwarded_chain:
        selected = _select_client_from_chain(forwarded_chain, trusted_networks=trusted_networks)
        if selected is None:
            return ClientSourceResolution(
                source="unknown",
                path="invalid_forwarding",
                had_forwarding_headers=True,
            )
        return ClientSourceResolution(
            source=selected,
            path="trusted_forwarded",
            had_forwarding_headers=True,
        )

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        # Vendor headers are ignored unless the X-Forwarded-For chain proves the
        # request transited Cloudflare (direct Render origin access cannot spoof).
        record_client_source_telemetry(
            ClientSourceResolution(
                source="unknown",
                path="invalid_forwarding",
                had_forwarding_headers=True,
            )
        )

    return ClientSourceResolution(
        source="unknown",
        path="unknown",
        had_forwarding_headers=had_forwarding_headers,
    )
