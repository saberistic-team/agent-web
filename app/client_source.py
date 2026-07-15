"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

_logger = logging.getLogger(__name__)

MAX_FORWARD_CHAIN_LENGTH = 32
_UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS = 60.0
_untrusted_forwarding_lock = threading.Lock()
_untrusted_forwarding_last_logged_at = 0.0

_WILDCARD_TRUST = "*"
_DEFAULT_TRUSTED_HOP_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network,
    ...,
] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

# Default Render load-balancer / private-network peer ranges for production.
RENDER_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
)

# Cloudflare published edge ranges — trusted hops only (not immediate-peer trust).
# https://www.cloudflare.com/ips/
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

_FORWARDED_FOR_TOKEN = re.compile(
    r"for=(?P<addr>(?:\"[^\"]+\")|\[[^\]]+\]|[^;,\s]+)",
    re.IGNORECASE,
)


class SourceResolutionPath(str, Enum):
    """Bounded telemetry labels for how client source was resolved."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_XFF_CHAIN = "trusted_xff_chain"
    TRUSTED_FORWARDED_HEADER = "trusted_forwarded_header"
    TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
    UNTRUSTED_FORWARDING = "untrusted_forwarding"
    INVALID_FORWARDING = "invalid_forwarding"
    MISSING_PEER = "missing_peer"


@dataclass(frozen=True)
class ClientSourceResult:
    """Resolved limiter source identity without retaining raw forwarding headers."""

    source: str
    path: SourceResolutionPath
    untrusted_forwarding_observed: bool = False


@dataclass(frozen=True)
class TrustedProxyConfig:
    """Parsed trusted-proxy boundary for one deployment."""

    cidrs: tuple[str, ...]
    trust_peer_wildcard: bool
    peer_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    hop_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @property
    def configured(self) -> bool:
        return bool(self.cidrs)


def _parse_network_entries(
    entries: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries:
        if entry == _WILDCARD_TRUST:
            continue
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                networks.append(
                    ipaddress.ip_network(
                        f"{entry}/{'128' if ':' in entry else '32'}",
                        strict=False,
                    )
                )
        except ValueError:
            continue
    return tuple(networks)


def build_trusted_proxy_config(cidrs: tuple[str, ...]) -> TrustedProxyConfig:
    """Build trusted networks from configured CIDR strings."""
    if not cidrs:
        return TrustedProxyConfig(
            cidrs=(),
            trust_peer_wildcard=False,
            peer_networks=(),
            hop_networks=(),
        )

    explicit_networks = _parse_network_entries(cidrs)
    cloudflare_hops = _parse_network_entries(CLOUDFLARE_EDGE_CIDRS)
    if _WILDCARD_TRUST in cidrs:
        hop_networks = _DEFAULT_TRUSTED_HOP_NETWORKS + explicit_networks + cloudflare_hops
        return TrustedProxyConfig(
            cidrs=cidrs,
            trust_peer_wildcard=True,
            peer_networks=(),
            hop_networks=hop_networks,
        )

    hop_networks = explicit_networks + cloudflare_hops
    return TrustedProxyConfig(
        cidrs=cidrs,
        trust_peer_wildcard=False,
        peer_networks=explicit_networks,
        hop_networks=hop_networks,
    )


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Parse comma-separated trusted proxy CIDRs/IPs from environment."""
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(entries)


def deployment_trust_summary(
    *,
    trusted_proxy_cidrs: tuple[str, ...],
    uvicorn_proxy_headers: bool,
    uvicorn_forwarded_allow_ips: str,
) -> dict[str, object]:
    """Non-sensitive deployment summary for /health verification."""
    config = build_trusted_proxy_config(trusted_proxy_cidrs)
    return {
        "trusted_proxies_configured": config.configured,
        "trust_wildcard": config.trust_peer_wildcard,
        "uvicorn_proxy_headers": uvicorn_proxy_headers,
        "uvicorn_forwarded_allow_ips": uvicorn_forwarded_allow_ips,
        "resolution_mode": "trusted_hop_chain" if config.configured else "direct_peer",
    }


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4, IPv6, ports, and IPv4-mapped IPv6 deterministically."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, port = candidate[1:].partition("]")
        candidate = host
        if port.startswith(":") and not port[1:].isdigit():
            return None
    elif candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host
        else:
            return None

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed


def _address_in_hop_networks(
    address: str,
    *,
    config: TrustedProxyConfig,
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in config.hop_networks)


def peer_is_trusted(peer_host: str | None, *, config: TrustedProxyConfig) -> bool:
    if not config.configured or not peer_host:
        return False
    if config.trust_peer_wildcard:
        return True
    normalized = normalize_ip_address(peer_host)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in config.peer_networks)


def parse_x_forwarded_for(value: str | None) -> list[str] | None:
    """Parse X-Forwarded-For; return None when malformed or overlong."""
    if not value:
        return []
    parts = [segment.strip() for segment in value.split(",")]
    if len(parts) > MAX_FORWARD_CHAIN_LENGTH:
        return None
    normalized: list[str] = []
    for part in parts:
        if not part:
            return None
        address = normalize_ip_address(part)
        if address is None:
            return None
        normalized.append(address)
    return normalized


def parse_forwarded_header(value: str | None) -> str | None:
    """Extract the first ``for=`` address from an RFC 7239 Forwarded header."""
    if not value:
        return None
    match = _FORWARDED_FOR_TOKEN.search(value)
    if match is None:
        return None
    token = match.group("addr").strip().strip('"')
    if token.startswith("[") and token.endswith("]"):
        token = token[1:-1]
    return normalize_ip_address(token)


def resolve_client_from_trusted_xff(
    *,
    peer_host: str,
    chain: list[str],
    config: TrustedProxyConfig,
) -> str | None:
    """Walk X-Forwarded-For right-to-left across trusted proxy hops."""
    peer = normalize_ip_address(peer_host)
    if peer is None:
        return None

    hops = list(chain)
    if not hops or hops[-1] != peer:
        hops.append(peer)

    for address in reversed(hops):
        if _address_in_hop_networks(address, config=config):
            continue
        return address

    return hops[0] if hops else peer


def resolve_client_source(
    *,
    peer_host: str | None,
    trusted_proxy_cidrs: tuple[str, ...],
    x_forwarded_for: str | None = None,
    forwarded: str | None = None,
    cf_connecting_ip: str | None = None,
) -> ClientSourceResult:
    """Resolve limiter source identity with a conservative trusted-proxy model."""
    config = build_trusted_proxy_config(trusted_proxy_cidrs)
    forwarding_present = any(
        header
        for header in (
            x_forwarded_for,
            forwarded,
            cf_connecting_ip,
        )
        if header and header.strip()
    )

    if peer_host is None:
        return ClientSourceResult(
            source="unknown",
            path=SourceResolutionPath.MISSING_PEER,
            untrusted_forwarding_observed=forwarding_present,
        )

    peer = normalize_ip_address(peer_host)
    if peer is None:
        raw_peer = peer_host.strip().lower()
        if not raw_peer:
            return ClientSourceResult(
                source="unknown",
                path=SourceResolutionPath.MISSING_PEER,
                untrusted_forwarding_observed=forwarding_present,
            )
        if not config.configured:
            return ClientSourceResult(
                source=raw_peer,
                path=SourceResolutionPath.DIRECT_PEER,
                untrusted_forwarding_observed=forwarding_present,
            )
        return ClientSourceResult(
            source=raw_peer,
            path=SourceResolutionPath.INVALID_FORWARDING,
            untrusted_forwarding_observed=True,
        )

    if not peer_is_trusted(peer, config=config):
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.DIRECT_PEER,
            untrusted_forwarding_observed=forwarding_present,
        )

    xff_chain = parse_x_forwarded_for(x_forwarded_for)
    if x_forwarded_for and xff_chain is None:
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.INVALID_FORWARDING,
            untrusted_forwarding_observed=True,
        )

    if xff_chain:
        resolved = resolve_client_from_trusted_xff(
            peer_host=peer,
            chain=xff_chain,
            config=config,
        )
        if resolved is not None:
            cf_candidate = (
                normalize_ip_address(cf_connecting_ip) if cf_connecting_ip else None
            )
            if cf_candidate and cf_candidate == resolved:
                return ClientSourceResult(
                    source=resolved,
                    path=SourceResolutionPath.TRUSTED_CF_CONNECTING_IP,
                )
            return ClientSourceResult(
                source=resolved,
                path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
            )

    forwarded_client = parse_forwarded_header(forwarded)
    if forwarded and forwarded_client is None:
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.INVALID_FORWARDING,
            untrusted_forwarding_observed=True,
        )
    if forwarded_client is not None:
        return ClientSourceResult(
            source=forwarded_client,
            path=SourceResolutionPath.TRUSTED_FORWARDED_HEADER,
        )

    if cf_connecting_ip:
        return ClientSourceResult(
            source=peer,
            path=SourceResolutionPath.UNTRUSTED_FORWARDING,
            untrusted_forwarding_observed=True,
        )

    return ClientSourceResult(
        source=peer,
        path=SourceResolutionPath.TRUSTED_XFF_CHAIN,
    )


def resolve_request_client_source(
    request: object,
    *,
    trusted_proxy_cidrs: tuple[str, ...],
) -> ClientSourceResult:
    """Resolve client source from a Starlette/FastAPI request."""
    peer_host = None
    client = getattr(request, "client", None)
    if client is not None:
        peer_host = getattr(client, "host", None)

    headers = getattr(request, "headers", None)
    get_header = headers.get if headers is not None else lambda _name, default="": default

    return resolve_client_source(
        peer_host=peer_host,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        x_forwarded_for=get_header("x-forwarded-for", ""),
        forwarded=get_header("forwarded", ""),
        cf_connecting_ip=get_header("cf-connecting-ip", ""),
    )


def record_client_source_telemetry(result: ClientSourceResult) -> None:
    """Emit bounded operational telemetry without raw addresses or header chains."""
    if result.path in {
        SourceResolutionPath.UNTRUSTED_FORWARDING,
        SourceResolutionPath.INVALID_FORWARDING,
    }:
        _maybe_log_untrusted_forwarding(result.path)

    _logger.debug(
        "Admin login client source resolved",
        extra={
            "source_resolution_path": result.path.value,
            "untrusted_forwarding_observed": result.untrusted_forwarding_observed,
        },
    )


def _maybe_log_untrusted_forwarding(path: SourceResolutionPath) -> None:
    global _untrusted_forwarding_last_logged_at
    now = time.monotonic()
    with _untrusted_forwarding_lock:
        if now - _untrusted_forwarding_last_logged_at < _UNTRUSTED_FORWARDING_LOG_INTERVAL_SECONDS:
            return
        _untrusted_forwarding_last_logged_at = now
    _logger.warning(
        "Ignored untrusted or invalid admin login forwarding headers",
        extra={"source_resolution_path": path.value},
    )
