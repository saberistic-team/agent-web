"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Conservative upper bound for comma-separated forwarding chains.
_MAX_FORWARDED_CHAIN_LENGTH = 32

# Sampled operational telemetry for invalid/untrusted forwarding attempts.
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_telemetry_lock = Lock()
_last_invalid_telemetry_at = 0.0
_last_untrusted_telemetry_at = 0.0

_logger = logging.getLogger(__name__)

# Render load balancers and loopback; production sets the same list on Uvicorn.
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
    "fc00::/7",
)

# Representative Cloudflare IPv4 ranges for CF-Connecting-IP validation only.
DEFAULT_CLOUDFLARE_PROXY_CIDRS: tuple[str, ...] = (
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
)

_FORWARDED_FOR_TOKEN = re.compile(
    r"^for=(?P<value>(?:\"[^\"]+\")|\S+)",
    re.IGNORECASE,
)



def parse_cidr_list(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated CIDR list, ignoring empty tokens."""
    if not raw.strip():
        return ()
    return tuple(token.strip() for token in raw.split(",") if token.strip())


class SourceResolutionPath(StrEnum):
    """Bounded telemetry labels; never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    FORWARDED_CHAIN = "forwarded_chain"
    FORWARDED_RFC = "forwarded_rfc"
    CF_CONNECTING_IP = "cf_connecting_ip"
    UNKNOWN = "unknown"
    UNTRUSTED_PEER = "untrusted_peer"
    INVALID_HEADERS = "invalid_headers"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source material and the path used to derive it."""

    source: str
    path: SourceResolutionPath


def effective_trusted_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    """Return configured trusted-proxy CIDRs, with legacy boolean fallback."""
    if settings.admin_trusted_proxy_cidrs:
        return settings.admin_trusted_proxy_cidrs
    if settings.admin_trust_proxy_headers:
        return DEFAULT_TRUSTED_PROXY_CIDRS
    return ()


def effective_cloudflare_proxy_cidrs(settings: Settings) -> tuple[str, ...]:
    if settings.admin_cloudflare_proxy_cidrs:
        return settings.admin_cloudflare_proxy_cidrs
    return DEFAULT_CLOUDFLARE_PROXY_CIDRS


def _compile_networks(cidrs: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _strip_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if value.count(":") == 1 and "." in value:
        host, _port = value.rsplit(":", 1)
        if _port.isdigit():
            return host
    return value


def _unwrap_quoted(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == '"' and trimmed[-1] == '"':
        return trimmed[1:-1].strip()
    return trimmed


def normalize_client_address(raw: str | None) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed input."""
    if raw is None:
        return None
    candidate = _unwrap_quoted(_strip_port(raw.strip()))
    if not candidate or len(candidate) > 128:
        return None
    if candidate.lower() == "unknown":
        return "unknown"
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address in network for network in networks)


def is_trusted_proxy_address(
    address: str,
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None or normalized == "unknown":
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return _address_in_networks(parsed, trusted_networks)


def parse_x_forwarded_for_chain(header_value: str) -> list[str] | None:
    """Parse X-Forwarded-For elements; return None when malformed or overlong."""
    if not header_value.strip():
        return None
    elements = [part.strip() for part in header_value.split(",")]
    if not elements or len(elements) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None
    if any(not element for element in elements):
        return None
    return elements


def resolve_client_from_forwarded_chain(
    chain: list[str],
    *,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    """Walk a forwarding chain right-to-left, skipping trusted proxy hops."""
    for raw_hop in reversed(chain):
        normalized = normalize_client_address(raw_hop)
        if normalized is None:
            return None
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            return None
        if _address_in_networks(parsed, trusted_networks):
            continue
        return normalized
    return None


def parse_forwarded_header_chain(header_value: str) -> list[str] | None:
    """Extract ``for=`` values from a Forwarded header chain."""
    if not header_value.strip():
        return None
    entries = [part.strip() for part in header_value.split(",")]
    if not entries or len(entries) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None
    values: list[str] = []
    for entry in entries:
        if not entry:
            return None
        match = _FORWARDED_FOR_TOKEN.search(entry)
        if match is None:
            return None
        values.append(_unwrap_quoted(match.group("value")))
    return values


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    raw = request.client.host
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized
    stripped = raw.strip().lower()
    return stripped or None


def _forwarded_hop_networks(
    settings: Settings,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Networks stripped while walking forwarding chains (Render LB + Cloudflare edge)."""
    hop_cidrs = (
        *effective_trusted_proxy_cidrs(settings),
        *effective_cloudflare_proxy_cidrs(settings),
    )
    return _compile_networks(hop_cidrs)


def _chain_contains_cloudflare_hop(
    chain: list[str],
    *,
    cloudflare_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            continue
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            continue
        if _address_in_networks(parsed, cloudflare_networks):
            return True
    return False


def _emit_sampled_telemetry(path: SourceResolutionPath) -> None:
    global _last_invalid_telemetry_at, _last_untrusted_telemetry_at
    now = time.monotonic()
    with _telemetry_lock:
        if path is SourceResolutionPath.INVALID_HEADERS:
            if now - _last_invalid_telemetry_at < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
                return
            _last_invalid_telemetry_at = now
        elif path is SourceResolutionPath.UNTRUSTED_PEER:
            if now - _last_untrusted_telemetry_at < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
                return
            _last_untrusted_telemetry_at = now
        else:
            return
    _logger.info(
        "Admin login client source resolution anomaly",
        extra={"resolution_path": path.value},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Trust model (production: Cloudflare → Render load balancer → Uvicorn):

    1. The immediate TCP peer must fall within ``ADMIN_TRUSTED_PROXY_CIDRS`` before
       any forwarding header can influence the limiter source.
    2. ``X-Forwarded-For`` is parsed right-to-left; trusted proxy hops are stripped
       and the rightmost non-trusted hop becomes the client source. The leftmost
       value is never selected directly.
    3. When ``X-Forwarded-For`` is absent or unusable, ``Forwarded`` (RFC 7239) is
       parsed with the same right-to-left trusted-hop walk.
    4. ``CF-Connecting-IP`` is used only when the peer is trusted *and* the
       ``X-Forwarded-For`` chain contains a Cloudflare edge hop, so direct Render
       origin requests cannot spoof a Cloudflare-derived address.
    5. Otherwise the normalized immediate peer is used; missing peers become
       ``unknown``.
    """
    trusted_cidrs = effective_trusted_proxy_cidrs(settings)
    trusted_networks = _compile_networks(trusted_cidrs)
    hop_networks = _forwarded_hop_networks(settings)
    cloudflare_networks = _compile_networks(effective_cloudflare_proxy_cidrs(settings))

    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path=SourceResolutionPath.UNKNOWN)

    if not trusted_networks or not is_trusted_proxy_address(
        peer,
        trusted_networks=trusted_networks,
    ):
        xff_present = bool(request.headers.get("x-forwarded-for", "").strip())
        forwarded_present = bool(request.headers.get("forwarded", "").strip())
        cf_present = bool(request.headers.get("cf-connecting-ip", "").strip())
        if xff_present or forwarded_present or cf_present:
            _emit_sampled_telemetry(SourceResolutionPath.UNTRUSTED_PEER)
        return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)

    xff_header = request.headers.get("x-forwarded-for", "")
    xff_chain = parse_x_forwarded_for_chain(xff_header)
    if xff_chain is not None:
        resolved = resolve_client_from_forwarded_chain(
            xff_chain,
            trusted_networks=hop_networks,
        )
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=SourceResolutionPath.FORWARDED_CHAIN,
            )
        _emit_sampled_telemetry(SourceResolutionPath.INVALID_HEADERS)
    elif xff_header.strip():
        _emit_sampled_telemetry(SourceResolutionPath.INVALID_HEADERS)

    forwarded_header = request.headers.get("forwarded", "")
    forwarded_chain = parse_forwarded_header_chain(forwarded_header)
    if forwarded_chain is not None:
        resolved = resolve_client_from_forwarded_chain(
            forwarded_chain,
            trusted_networks=hop_networks,
        )
        if resolved is not None:
            return ClientSourceResolution(
                source=resolved,
                path=SourceResolutionPath.FORWARDED_RFC,
            )
        _emit_sampled_telemetry(SourceResolutionPath.INVALID_HEADERS)
    elif forwarded_header.strip():
        _emit_sampled_telemetry(SourceResolutionPath.INVALID_HEADERS)

    cf_header = request.headers.get("cf-connecting-ip", "")
    cf_candidate = normalize_client_address(cf_header) if cf_header.strip() else None
    if (
        cf_candidate is not None
        and xff_chain is not None
        and _chain_contains_cloudflare_hop(
            xff_chain,
            cloudflare_networks=cloudflare_networks,
        )
    ):
        return ClientSourceResolution(
            source=cf_candidate,
            path=SourceResolutionPath.CF_CONNECTING_IP,
        )
    if cf_header.strip() and (
        xff_chain is None
        or not _chain_contains_cloudflare_hop(
            xff_chain,
            cloudflare_networks=cloudflare_networks,
        )
    ):
        _emit_sampled_telemetry(SourceResolutionPath.UNTRUSTED_PEER)

    return ClientSourceResolution(source=peer, path=SourceResolutionPath.DIRECT_PEER)


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).source
