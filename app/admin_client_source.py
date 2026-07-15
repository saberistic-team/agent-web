"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Render internal load balancers and RFC1918/CGNAT hops in the production chain.
_RENDER_INTERNAL_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
)

# Published Cloudflare IPv4 ranges (https://www.cloudflare.com/ips-v4).
_CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
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

DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = _RENDER_INTERNAL_CIDRS + _CLOUDFLARE_IPV4_CIDRS

RENDER_FORWARDED_ALLOW_IPS: str = ",".join(_RENDER_INTERNAL_CIDRS)
PRODUCTION_TRUSTED_PROXY_CIDRS: str = ",".join(DEFAULT_TRUSTED_PROXY_CIDRS)

MAX_FORWARDED_CHAIN_LENGTH = 32
_INVALID_FORWARDED_LOG_INTERVAL_SECONDS = 60.0

_logger = logging.getLogger(__name__)
_last_invalid_forwarded_log_monotonic = 0.0
_suppressed_invalid_forwarded_count = 0

# RFC 7239 Forwarded: for="203.0.113.60" or for=203.0.113.60
_FORWARDED_FOR_RE = re.compile(
    r'for=(?:"\[?(?P<quoted>[^\]"]+)\]?"|(?P<plain>[^;,]+))',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the path used to derive it."""

    source: str
    path: str


def reset_client_source_telemetry() -> None:
    """Clear sampled invalid-forwarding counters (tests only)."""
    global _last_invalid_forwarded_log_monotonic, _suppressed_invalid_forwarded_count
    _last_invalid_forwarded_log_monotonic = 0.0
    _suppressed_invalid_forwarded_count = 0


def parse_trusted_proxy_cidrs(raw: str | None) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse comma-separated CIDRs/IPs into networks; ignore invalid entries."""
    if not raw or not raw.strip():
        return ()
    networks: list[ipaddress._BaseNetwork] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                networks.append(
                    ipaddress.ip_network(f"{token}/32", strict=False)
                    if ":" not in token
                    else ipaddress.ip_network(f"{token}/128", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)


def trusted_proxy_networks(settings: Settings) -> tuple[ipaddress._BaseNetwork, ...]:
    """Return configured trusted proxy networks for the active settings."""
    if settings.admin_trusted_proxy_cidrs:
        parsed = parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
        if parsed:
            return parsed
    if settings.admin_trust_proxy_headers:
        return tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_TRUSTED_PROXY_CIDRS
        )
    return ()


def cloudflare_networks(settings: Settings) -> tuple[ipaddress._BaseNetwork, ...]:
    """Cloudflare edge ranges used to validate vendor-specific headers."""
    if settings.admin_cloudflare_proxy_cidrs:
        parsed = parse_trusted_proxy_cidrs(settings.admin_cloudflare_proxy_cidrs)
        if parsed:
            return parsed
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in _CLOUDFLARE_IPV4_CIDRS)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) or return None when invalid."""
    text = raw.strip()
    if not text or len(text) > 128:
        return None

    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            return None
        host = text[1:closing]
        remainder = text[closing + 1 :]
        if remainder.startswith(":") and remainder[1:].isdigit():
            text = host
        else:
            text = host
    elif text.count(":") == 1 and "." in text:
        host, _, port = text.rpartition(":")
        if port.isdigit():
            text = host

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return address.compressed.lower()


def _effective_source(raw: str | None) -> str:
    if raw is None:
        return "unknown"
    normalized = normalize_client_address(raw)
    if normalized is not None:
        return normalized
    stripped = raw.strip()
    return stripped if stripped else "unknown"


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    return host.strip() if host else None


def _address_in_networks(
    address: str,
    networks: Iterable[ipaddress._BaseNetwork],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def _is_trusted_proxy(address: str, trusted_networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    return bool(trusted_networks) and _address_in_networks(address, trusted_networks)


def _parse_x_forwarded_for(header_value: str) -> tuple[list[str], bool]:
    """Return parsed addresses and whether the header was structurally valid."""
    if not header_value.strip():
        return [], False
    if len(header_value) > 4096:
        return [], False

    parsed: list[str] = []
    for element in header_value.split(","):
        candidate = element.strip()
        if not candidate:
            return [], False
        if len(parsed) >= MAX_FORWARDED_CHAIN_LENGTH:
            return [], False
        normalized = normalize_client_address(candidate)
        if normalized is None:
            return [], False
        parsed.append(normalized)
    return parsed, bool(parsed)


def _parse_forwarded_header(header_value: str) -> tuple[list[str], bool]:
    if not header_value.strip():
        return [], False
    if len(header_value) > 4096:
        return [], False

    parsed: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        if len(parsed) >= MAX_FORWARDED_CHAIN_LENGTH:
            return [], False
        raw = (match.group("quoted") or match.group("plain") or "").strip()
        if not raw or raw.lower() == "unknown":
            continue
        normalized = normalize_client_address(raw)
        if normalized is None:
            return [], False
        parsed.append(normalized)
    return parsed, bool(parsed)


def _client_from_trusted_chain(
    chain: list[str],
    *,
    immediate_peer: str,
    trusted_networks: tuple[ipaddress._BaseNetwork, ...],
) -> str | None:
    """Select the right-most untrusted hop, treating the immediate peer as chain tail."""
    if not trusted_networks:
        return None

    full_chain = list(chain)
    peer_normalized = normalize_client_address(immediate_peer) or immediate_peer.strip()
    if peer_normalized and (
        not full_chain or full_chain[-1].lower() != peer_normalized.lower()
    ):
        full_chain.append(peer_normalized)

    for hop in reversed(full_chain):
        if _is_trusted_proxy(hop, trusted_networks):
            continue
        return hop
    return None


def _cloudflare_hop_present(
    chain: list[str],
    *,
    immediate_peer: str,
    cloudflare_networks: tuple[ipaddress._BaseNetwork, ...],
) -> bool:
    peer = normalize_client_address(immediate_peer) or immediate_peer.strip()
    candidates = list(chain)
    if peer:
        candidates.append(peer)
    return any(_address_in_networks(hop, cloudflare_networks) for hop in candidates)


def _record_resolution(path: str) -> None:
    _logger.debug(
        "Admin login client source resolved",
        extra={"client_source_path": path},
    )


def _record_invalid_forwarding(path: str) -> None:
    global _last_invalid_forwarded_log_monotonic, _suppressed_invalid_forwarded_count
    now = time.monotonic()
    if now - _last_invalid_forwarded_log_monotonic >= _INVALID_FORWARDED_LOG_INTERVAL_SECONDS:
        extra: dict[str, object] = {"client_source_path": path}
        if _suppressed_invalid_forwarded_count:
            extra["suppressed_count"] = _suppressed_invalid_forwarded_count
        _logger.info("Admin login untrusted forwarding header", extra=extra)
        _last_invalid_forwarded_log_monotonic = now
        _suppressed_invalid_forwarded_count = 0
    else:
        _suppressed_invalid_forwarded_count += 1


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented): browser → Cloudflare edge → Render load
    balancer → Uvicorn. Forwarding headers are parsed only when the immediate
    peer matches ``ADMIN_TRUSTED_PROXY_CIDRS``. The right-most untrusted hop in
    ``X-Forwarded-For`` (Render appends the connecting address) is the client.
    Vendor headers such as ``CF-Connecting-IP`` are accepted only when a
    Cloudflare edge address appears in the verified forwarding chain.
    """
    immediate_peer = _immediate_peer_host(request)
    peer_source = _effective_source(immediate_peer)

    if not settings.admin_trust_proxy_headers:
        resolution = ClientSourceResolution(source=peer_source, path="direct_peer")
        _record_resolution(resolution.path)
        return resolution

    trusted_networks = trusted_proxy_networks(settings)
    if not immediate_peer or not _is_trusted_proxy(immediate_peer, trusted_networks):
        if immediate_peer and request.headers.get("x-forwarded-for"):
            _record_invalid_forwarding("untrusted_peer_ignored_forwarding")
        resolution = ClientSourceResolution(source=peer_source, path="untrusted_peer")
        _record_resolution(resolution.path)
        return resolution

    xff_chain, xff_valid = _parse_x_forwarded_for(
        request.headers.get("x-forwarded-for", "")
    )
    if request.headers.get("x-forwarded-for") and not xff_valid:
        _record_invalid_forwarding("invalid_x_forwarded_for")

    client = None
    if xff_valid:
        client = _client_from_trusted_chain(
            xff_chain,
            immediate_peer=immediate_peer,
            trusted_networks=trusted_networks,
        )
        if client is not None:
            resolution = ClientSourceResolution(source=client, path="xff_trusted_chain")
            _record_resolution(resolution.path)
            return resolution

    forwarded_chain, forwarded_valid = _parse_forwarded_header(
        request.headers.get("forwarded", "")
    )
    if request.headers.get("forwarded") and not forwarded_valid:
        _record_invalid_forwarding("invalid_forwarded_header")

    if forwarded_valid and not xff_valid:
        client = _client_from_trusted_chain(
            forwarded_chain,
            immediate_peer=immediate_peer,
            trusted_networks=trusted_networks,
        )
        if client is not None:
            resolution = ClientSourceResolution(
                source=client,
                path="forwarded_trusted_chain",
            )
            _record_resolution(resolution.path)
            return resolution

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header and not xff_valid and not forwarded_valid:
        cf_networks = cloudflare_networks(settings)
        combined_chain = xff_chain or forwarded_chain
        if _cloudflare_hop_present(
            combined_chain,
            immediate_peer=immediate_peer,
            cloudflare_networks=cf_networks,
        ):
            cf_client = normalize_client_address(cf_header)
            if cf_client is not None:
                resolution = ClientSourceResolution(
                    source=cf_client,
                    path="cf_connecting_ip_verified",
                )
                _record_resolution(resolution.path)
                return resolution
        _record_invalid_forwarding("cf_connecting_ip_unverified")
    elif cf_header and (xff_valid or forwarded_valid):
        _record_invalid_forwarding("cf_connecting_ip_precedence_skipped")

    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        _record_invalid_forwarding("trusted_peer_no_client_hop")

    resolution = ClientSourceResolution(source=peer_source, path="trusted_peer_fallback")
    _record_resolution(resolution.path)
    return resolution


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for admin login limiters."""
    return resolve_admin_login_client_source(request, settings).source
