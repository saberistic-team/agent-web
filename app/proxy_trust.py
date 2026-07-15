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

_logger = logging.getLogger(__name__)

# Conservative upper bound for comma-separated forwarding chains.
_MAX_FORWARDING_CHAIN_LENGTH = 20

# Sample one operational log line per N invalid/untrusted forwarding attempts.
_INVALID_FORWARDING_LOG_SAMPLE_RATE = 100

# Published Cloudflare edge networks (IPv4 + IPv6) for hop-skipping and
# CF-Connecting-IP validation. Update when Cloudflare publishes changes.
_DEFAULT_CLOUDFLARE_EDGE_NETWORKS: tuple[str, ...] = (
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

# Render / private-network immediate peers in the Cloudflare → Render → Uvicorn chain.
_DEFAULT_RENDER_TRUSTED_PROXY_NETWORKS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)

_FORWARDED_FOR_PARAM = re.compile(
    r"""for=(?:"\[([^\]]+)\]"|([^;,\s"]+))""",
    re.IGNORECASE,
)

_invalid_forwarding_counter = 0
_invalid_forwarding_lock = threading.Lock()


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved login limiter source identity without persisting raw addresses."""

    source: str
    path: str
    invalid_forwarding: bool = False


def default_trusted_proxy_networks() -> tuple[str, ...]:
    """Production Render private peers plus Cloudflare edge networks for hop-skipping."""
    return _DEFAULT_RENDER_TRUSTED_PROXY_NETWORKS + _DEFAULT_CLOUDFLARE_EDGE_NETWORKS


def default_trusted_proxy_ips_spec() -> str:
    """Comma-separated default for ``ADMIN_TRUSTED_PROXY_IPS`` / Uvicorn."""
    return ",".join(default_trusted_proxy_networks())


def default_cloudflare_edge_networks() -> tuple[str, ...]:
    return _DEFAULT_CLOUDFLARE_EDGE_NETWORKS


def parse_network_spec(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs and host IPs; ignore malformed entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in spec.split(","):
        entry = item.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            try:
                host = ipaddress.ip_address(entry)
            except ValueError:
                continue
            networks.append(
                ipaddress.ip_network(f"{host}/{host.max_prefixlen}", strict=False)
            )
    return tuple(networks)


def parse_host_port(value: str) -> tuple[str, int | None]:
    """Split a forwarding hop into host and optional port."""
    trimmed = value.strip()
    if not trimmed:
        return "", None

    if trimmed.startswith("["):
        bracket_end = trimmed.find("]")
        if bracket_end == -1:
            return trimmed, None
        host = trimmed[1:bracket_end]
        remainder = trimmed[bracket_end + 1 :]
        if not remainder:
            return host, None
        if not remainder.startswith(":"):
            return trimmed, None
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, None

    if trimmed.count(":") == 1 and "." in trimmed:
        host, port_text = trimmed.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return trimmed, None

    return trimmed, None


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (including IPv4-mapped) or return None when invalid."""
    host, _ = parse_host_port(raw)
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed


def is_address_in_networks(
    address: str,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _split_forwarding_chain(header_value: str) -> list[str] | None:
    hops = [item.strip() for item in header_value.split(",")]
    hops = [hop for hop in hops if hop]
    if len(hops) > _MAX_FORWARDING_CHAIN_LENGTH:
        return None
    return hops


@dataclass(frozen=True)
class _XffParseResult:
    source: str | None
    malformed: bool = False


def _resolve_from_x_forwarded_for(
    header_value: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> _XffParseResult:
    hops = _split_forwarding_chain(header_value)
    if hops is None:
        return _XffParseResult(source=None, malformed=True)
    if not hops:
        return _XffParseResult(source=None, malformed=True)

    normalized_hops: list[str] = []
    for hop in hops:
        host, _ = parse_host_port(hop)
        normalized = normalize_client_address(host)
        if normalized is None:
            return _XffParseResult(source=None, malformed=True)
        normalized_hops.append(normalized)

    for candidate in reversed(normalized_hops):
        if not is_address_in_networks(candidate, trusted_networks):
            return _XffParseResult(source=candidate, malformed=False)

    # Every hop is a trusted proxy — defer to vendor-specific headers.
    return _XffParseResult(source=None, malformed=False)


def _cloudflare_edge_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    spec = settings.admin_cloudflare_edge_ips.strip()
    if spec:
        return parse_network_spec(spec)
    return parse_network_spec(",".join(default_cloudflare_edge_networks()))


def _trusted_proxy_networks(settings: Settings) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    spec = settings.admin_trusted_proxy_ips.strip()
    if spec:
        return parse_network_spec(spec)
    if settings.admin_trust_proxy_headers:
        return parse_network_spec(",".join(default_trusted_proxy_networks()))
    return ()


def _parse_forwarded_header(
    header_value: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if len(header_value) > 4096:
        return None

    candidates: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_PARAM.search(entry)
        if not match:
            continue
        raw_host = match.group(1) or match.group(2) or ""
        host, _ = parse_host_port(raw_host.strip())
        normalized = normalize_client_address(host)
        if normalized is None:
            return None
        candidates.append(normalized)

    if not candidates:
        return None

    for candidate in reversed(candidates):
        if not is_address_in_networks(candidate, trusted_networks):
            return candidate
    return None


def _resolve_cf_connecting_ip(
    request: Request,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    raw = request.headers.get("cf-connecting-ip", "").strip()
    if not raw:
        return None

    normalized = normalize_client_address(raw)
    if normalized is None:
        return None

    xff = request.headers.get("x-forwarded-for", "").strip()
    if not xff:
        return None

    hops = _split_forwarding_chain(xff)
    if not hops:
        return None

    rightmost_host, _ = parse_host_port(hops[-1])
    if not is_address_in_networks(rightmost_host, cloudflare_networks):
        return None

    # Require at least one trusted Render hop between the app and the edge chain.
    if not is_address_in_networks(_immediate_peer_host(request) or "", trusted_networks):
        return None

    return normalized


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    normalized = normalize_client_address(request.client.host)
    if normalized is not None:
        return normalized
    host = request.client.host.strip()
    return host or None


def _has_forwarding_headers(request: Request) -> bool:
    header_names = {
        "x-forwarded-for",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    }
    return any(name in request.headers for name in header_names)


def _maybe_log_invalid_forwarding(path: str) -> None:
    global _invalid_forwarding_counter
    with _invalid_forwarding_lock:
        _invalid_forwarding_counter += 1
        count = _invalid_forwarding_counter
    if count % _INVALID_FORWARDING_LOG_SAMPLE_RATE == 1:
        _logger.info(
            "Admin login source resolution rejected forwarding headers",
            extra={"resolution_path": path, "sampled_count": count},
        )


def reset_invalid_forwarding_telemetry() -> None:
    """Clear invalid-forwarding sampling counter (tests only)."""
    global _invalid_forwarding_counter
    with _invalid_forwarding_lock:
        _invalid_forwarding_counter = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented): public client → Cloudflare edge → Render load
    balancer → Uvicorn (``ProxyHeadersMiddleware`` with ``--forwarded-allow-ips``).

    Precedence when ``ADMIN_TRUST_PROXY_HEADERS`` is enabled and the immediate peer
    is a configured trusted proxy:

    1. ``X-Forwarded-For`` parsed right-to-left, skipping trusted proxy hops.
    2. ``CF-Connecting-IP`` when the rightmost ``X-Forwarded-For`` hop is a
       Cloudflare edge network (prevents direct Render origin spoofing).
    3. RFC 7239 ``Forwarded`` ``for=`` parameters with the same right-to-left rule.

    When proxy trust is disabled, or the immediate peer is not trusted, only the
    direct peer address is used and all forwarding headers are ignored.
    """
    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path="unknown_peer")

    if not settings.admin_trust_proxy_headers:
        if _has_forwarding_headers(request):
            _maybe_log_invalid_forwarding("untrusted_forwarding_disabled")
        return ClientSourceResolution(source=peer, path="direct_peer")

    trusted_networks = _trusted_proxy_networks(settings)
    if not trusted_networks:
        if _has_forwarding_headers(request):
            _maybe_log_invalid_forwarding("untrusted_forwarding_no_trusted_networks")
        return ClientSourceResolution(source=peer, path="direct_peer")

    if not is_address_in_networks(peer, trusted_networks):
        if _has_forwarding_headers(request):
            _maybe_log_invalid_forwarding("untrusted_forwarding_peer")
            return ClientSourceResolution(
                source=peer,
                path="untrusted_forwarding_peer",
                invalid_forwarding=True,
            )
        return ClientSourceResolution(source=peer, path="direct_peer")

    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        xff_result = _resolve_from_x_forwarded_for(xff, trusted_networks)
        if xff_result.malformed:
            _maybe_log_invalid_forwarding("malformed_x_forwarded_for")
            return ClientSourceResolution(
                source="unknown",
                path="malformed_x_forwarded_for",
                invalid_forwarding=True,
            )
        if xff_result.source is not None:
            return ClientSourceResolution(source=xff_result.source, path="xff_trusted_chain")

    cf_resolved = _resolve_cf_connecting_ip(
        request,
        trusted_networks=trusted_networks,
        cloudflare_networks=_cloudflare_edge_networks(settings),
    )
    if cf_resolved is not None:
        return ClientSourceResolution(source=cf_resolved, path="cf_connecting_ip")

    forwarded = request.headers.get("forwarded", "").strip()
    if forwarded:
        resolved = _parse_forwarded_header(forwarded, trusted_networks)
        if resolved is not None:
            return ClientSourceResolution(source=resolved, path="forwarded_header")
        _maybe_log_invalid_forwarding("malformed_forwarded")
        return ClientSourceResolution(
            source="unknown",
            path="malformed_forwarded",
            invalid_forwarding=True,
        )

    if _has_forwarding_headers(request):
        _maybe_log_invalid_forwarding("malformed_forwarding")
        return ClientSourceResolution(
            source="unknown",
            path="malformed_forwarding",
            invalid_forwarding=True,
        )

    return ClientSourceResolution(source=peer, path="direct_peer")


def proxy_trust_health_summary(settings: Settings) -> dict[str, object]:
    """Non-sensitive deployment verification payload for ``/health``."""
    trusted_networks = _trusted_proxy_networks(settings)
    forwarded_allow_ips = settings.uvicorn_forwarded_allow_ips.strip()
    if not forwarded_allow_ips:
        forwarded_allow_ips = settings.admin_trusted_proxy_ips.strip()
    return {
        "enabled": settings.admin_trust_proxy_headers,
        "trusted_network_count": len(trusted_networks),
        "forwarded_allow_ips": forwarded_allow_ips,
    }
