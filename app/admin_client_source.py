"""Verified-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

# Conservative cap on comma-separated forwarding chains (spoof amplification).
MAX_FORWARDED_CHAIN_LENGTH = 32

# Sampled telemetry for invalid/untrusted forwarding attempts (no raw IPs).
_INVALID_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_last_invalid_forwarding_log_at = 0.0

# Render private-network peers that terminate TLS before the app process.
DEFAULT_RENDER_TRUSTED_NETWORKS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

# Cloudflare published egress ranges (https://www.cloudflare.com/ips-v4).
# Used only to prove a request transited the public edge before honoring
# ``CF-Connecting-IP``. Update when Cloudflare publishes changes.
DEFAULT_CLOUDFLARE_TRUSTED_NETWORKS: tuple[str, ...] = (
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

TRUST_MODEL_VERSION = "verified-hop-v1"


@dataclass(frozen=True)
class ClientSourceResolution:
    """Limiter source identity plus a privacy-preserving resolution path label."""

    source: str
    path: str


@dataclass(frozen=True)
class _TrustedNetworks:
    always_trust: bool
    hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]
    literals: frozenset[str]


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled invalid-forwarding telemetry state (tests only)."""
    global _last_invalid_forwarding_log_at
    _last_invalid_forwarding_log_at = 0.0
    _trusted_networks_from_specs.cache_clear()


def trust_model_summary(settings: Settings) -> dict[str, str | bool]:
    """Non-sensitive deployment verification payload for ``/health``."""
    return {
        "admin_client_source_trust": TRUST_MODEL_VERSION,
        "admin_trust_proxy_headers": settings.admin_trust_proxy_headers,
        "admin_proxy_headers_enabled": False,
        "uvicorn_forwarded_allow_ips": settings.uvicorn_forwarded_allow_ips,
    }


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, and IPv4-mapped IPv6 deterministically."""
    candidate = raw.strip()
    if not candidate:
        return None

    host = candidate
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder:
            if not remainder.startswith(":"):
                return None
            port_text = remainder[1:]
            if not port_text.isdigit():
                return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port_text = candidate.partition(":")
        if not port_text.isdigit():
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


def _parse_network_specs(spec: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in spec.split(",") if part.strip())


@lru_cache(maxsize=8)
def _trusted_networks_from_specs(spec: str) -> _TrustedNetworks:
    if spec.strip() == "*":
        return _TrustedNetworks(True, frozenset(), frozenset(), frozenset())

    hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    literals: set[str] = set()

    for entry in _parse_network_specs(spec):
        if "/" in entry:
            try:
                networks.add(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                literals.add(entry)
            continue
        try:
            hosts.add(ipaddress.ip_address(entry))
        except ValueError:
            literals.add(entry)

    return _TrustedNetworks(False, frozenset(hosts), frozenset(networks), frozenset(literals))


def _is_trusted_host(host: str, trusted: _TrustedNetworks) -> bool:
    if trusted.always_trust:
        return True
    if not host:
        return False
    if len(host) > 253:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in trusted.literals
    if address in trusted.hosts:
        return True
    return any(address in network for network in trusted.networks)


def _trusted_networks_for_settings(settings: Settings) -> _TrustedNetworks:
    specs = list(DEFAULT_RENDER_TRUSTED_NETWORKS)
    if settings.admin_trusted_proxy_ips:
        specs.extend(_parse_network_specs(settings.admin_trusted_proxy_ips))
    else:
        specs.extend(DEFAULT_CLOUDFLARE_TRUSTED_NETWORKS)
    return _trusted_networks_from_specs(",".join(specs))


def _cloudflare_networks_for_settings(settings: Settings) -> _TrustedNetworks:
    specs = list(DEFAULT_CLOUDFLARE_TRUSTED_NETWORKS)
    if settings.admin_trusted_edge_proxy_ips:
        specs = list(_parse_network_specs(settings.admin_trusted_edge_proxy_ips))
    return _trusted_networks_from_specs(",".join(specs))


def _immediate_peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _split_forwarding_chain(header_value: str) -> list[str] | None:
    if not header_value.strip():
        return None
    parts = [segment.strip() for segment in header_value.split(",")]
    if not parts or any(not part for part in parts):
        return None
    if len(parts) > MAX_FORWARDED_CHAIN_LENGTH:
        return None
    return parts


def _client_from_trusted_chain(
    chain: list[str],
    trusted: _TrustedNetworks,
) -> str | None:
    for hop in reversed(chain):
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        if not _is_trusted_host(normalized, trusted):
            return normalized

    # Every hop is a trusted intermediary — fail closed to the leftmost hop.
    leftmost = normalize_client_address(chain[0])
    return leftmost


def _parse_forwarded_for_header(
    request: Request,
    trusted: _TrustedNetworks,
) -> ClientSourceResolution | None:
    header_value = request.headers.get("x-forwarded-for")
    if header_value is None:
        return None
    chain = _split_forwarding_chain(header_value)
    if chain is None:
        return ClientSourceResolution(source="unknown", path="invalid_forwarding")
    source = _client_from_trusted_chain(chain, trusted)
    if source is None:
        return ClientSourceResolution(source="unknown", path="invalid_forwarding")
    if all(
        _is_trusted_host(normalize_client_address(hop) or "", trusted) for hop in chain
    ):
        return ClientSourceResolution(source=source, path="ambiguous_trusted_chain")
    return ClientSourceResolution(source=source, path="trusted_xff")


def _parse_rfc7239_forwarded_header(
    request: Request,
    trusted: _TrustedNetworks,
) -> ClientSourceResolution | None:
    header_value = request.headers.get("forwarded")
    if header_value is None:
        return None

    chain: list[str] = []
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry:
            return ClientSourceResolution(source="unknown", path="invalid_forwarding")
        matched = False
        for part in entry.split(";"):
            part = part.strip()
            if not part.lower().startswith("for="):
                continue
            value = part[4:].strip().strip('"')
            if value.lower() in {"unknown", "[unknown]"}:
                return ClientSourceResolution(source="unknown", path="invalid_forwarding")
            if value.startswith("obfuscated"):
                return ClientSourceResolution(source="unknown", path="invalid_forwarding")
            chain.append(value)
            matched = True
            break
        if not matched:
            return ClientSourceResolution(source="unknown", path="invalid_forwarding")

    if not chain or len(chain) > MAX_FORWARDED_CHAIN_LENGTH:
        return ClientSourceResolution(source="unknown", path="invalid_forwarding")

    source = _client_from_trusted_chain(chain, trusted)
    if source is None:
        return ClientSourceResolution(source="unknown", path="invalid_forwarding")
    if all(
        _is_trusted_host(normalize_client_address(hop) or "", trusted) for hop in chain
    ):
        return ClientSourceResolution(source=source, path="ambiguous_trusted_chain")
    return ClientSourceResolution(source=source, path="trusted_forwarded")


def _cloudflare_edge_verified(request: Request, cf_trusted: _TrustedNetworks) -> bool:
    header_value = request.headers.get("x-forwarded-for")
    if header_value:
        chain = _split_forwarding_chain(header_value)
        if chain is None:
            return False
        for hop in chain:
            normalized = normalize_client_address(hop)
            if normalized and _is_trusted_host(normalized, cf_trusted):
                return True

    peer = _immediate_peer_host(request)
    if peer and _is_trusted_host(peer, cf_trusted):
        return True
    return False


def _parse_cf_connecting_ip(
    request: Request,
    *,
    trusted: _TrustedNetworks,
    cf_trusted: _TrustedNetworks,
) -> ClientSourceResolution | None:
    if not _cloudflare_edge_verified(request, cf_trusted):
        return None
    header_value = request.headers.get("cf-connecting-ip")
    if header_value is None:
        return None
    normalized = normalize_client_address(header_value.strip())
    if normalized is None:
        return ClientSourceResolution(source="unknown", path="invalid_forwarding")
    if _is_trusted_host(normalized, trusted):
        return None
    return ClientSourceResolution(source=normalized, path="trusted_cf_connecting_ip")


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _maybe_log_forwarding_telemetry(path: str) -> None:
    global _last_invalid_forwarding_log_at
    now = time.monotonic()
    if now - _last_invalid_forwarding_log_at < _INVALID_FORWARDING_LOG_INTERVAL_SECONDS:
        return
    _last_invalid_forwarding_log_at = now
    _logger.info(
        "Admin login client source resolution",
        extra={"client_source_path": path},
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve limiter source identity with verified-hop proxy trust.

    Production chain (saberistic.com): Internet → Cloudflare edge → Render load
    balancer → Uvicorn (``--proxy-headers`` intentionally disabled; see
    ``docs/ADMIN_AUTH.md``). Forwarding headers are parsed only when the
    immediate TCP peer is a configured trusted proxy.
    """
    peer = _immediate_peer_host(request)
    if peer is None:
        return ClientSourceResolution(source="unknown", path="missing_peer")

    peer_host = peer.strip()
    if not peer_host:
        return ClientSourceResolution(source="unknown", path="missing_peer")

    normalized_peer = normalize_client_address(peer_host)
    peer_trust_key = normalized_peer or peer_host
    direct_source = normalized_peer or peer_host

    if not settings.admin_trust_proxy_headers:
        if _has_forwarding_headers(request):
            _maybe_log_forwarding_telemetry("untrusted_forwarding_ignored")
        return ClientSourceResolution(source=direct_source, path="direct_peer")

    trusted = _trusted_networks_for_settings(settings)
    if not _is_trusted_host(peer_trust_key, trusted):
        if _has_forwarding_headers(request):
            _maybe_log_forwarding_telemetry("untrusted_forwarding_ignored")
        return ClientSourceResolution(source=direct_source, path="untrusted_peer")

    xff_resolution: ClientSourceResolution | None = None
    for parser in (
        lambda: _parse_forwarded_for_header(request, trusted),
        lambda: _parse_rfc7239_forwarded_header(request, trusted),
    ):
        resolution = parser()
        if resolution is None:
            continue
        if resolution.path in {"trusted_xff", "trusted_forwarded"}:
            return resolution
        if resolution.path == "invalid_forwarding":
            _maybe_log_forwarding_telemetry(resolution.path)
            return resolution
        if resolution.path == "ambiguous_trusted_chain":
            xff_resolution = resolution

    cf_resolution = _parse_cf_connecting_ip(
        request,
        trusted=trusted,
        cf_trusted=_cloudflare_networks_for_settings(settings),
    )
    if cf_resolution is not None:
        return cf_resolution

    if xff_resolution is not None:
        _maybe_log_forwarding_telemetry(xff_resolution.path)
        return xff_resolution

    return ClientSourceResolution(source=direct_source, path="direct_peer")


def client_ip(request: Request, settings: Settings) -> str:
    """Return the normalized limiter source string for one request."""
    return resolve_admin_login_client_source(request, settings).source
