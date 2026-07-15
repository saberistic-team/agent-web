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

# Render private-network ranges used by the platform load balancer → app hop.
# Must stay in sync with ``render.yaml`` ``--forwarded-allow-ips`` and
# ``ADMIN_TRUSTED_PROXY_CIDRS`` (enforced by deployment tests).
PRODUCTION_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "100.64.0.0/10",
)

# Conservative upper bound on comma-separated forwarding chains.
_MAX_FORWARD_CHAIN_LENGTH = 32

# Sample one operational warning per this many invalid/untrusted header events.
_INVALID_HEADER_SAMPLE_RATE = 100

_telemetry_lock = threading.Lock()
_invalid_header_event_count = 0

# Telemetry-only path labels (never include raw addresses or header values).
PATH_DIRECT_PEER = "direct_peer"
PATH_XFF_TRUSTED_WALK = "xff_trusted_walk"
PATH_FORWARDED_HEADER = "forwarded_header"
PATH_CF_CONNECTING_IP = "cf_connecting_ip"
PATH_CONSERVATIVE_UNKNOWN = "conservative_unknown"
PATH_CONSERVATIVE_UNTRUSTED_HEADERS = "conservative_untrusted_headers"
PATH_CONSERVATIVE_AMBIGUOUS = "conservative_ambiguous"

_FORWARDED_FOR_TOKEN_RE = re.compile(r"^for=(?:(?:\"([^\"]+)\")|([^;,\s]+))", re.IGNORECASE)


@dataclass(frozen=True)
class TrustedProxyBoundary:
    """Configured immediate-peer allowlist for forwarding-header trust."""

    hosts: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]
    literals: frozenset[str]

    @classmethod
    def from_cidrs(cls, cidrs: Iterable[str]) -> TrustedProxyBoundary:
        hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
        literals: set[str] = set()
        for raw in cidrs:
            entry = raw.strip()
            if not entry:
                continue
            if "/" in entry:
                try:
                    networks.add(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    literals.add(entry)
            else:
                try:
                    hosts.add(ipaddress.ip_address(entry))
                except ValueError:
                    literals.add(entry)
        return cls(
            hosts=frozenset(hosts),
            networks=frozenset(networks),
            literals=frozenset(literals),
        )

    @property
    def configured(self) -> bool:
        return bool(self.hosts or self.networks or self.literals)

    def trusts(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host in self.literals
        if ip in self.hosts:
            return True
        return any(ip in network for network in self.networks)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity without persisting forwarding metadata."""

    address: str
    path: str


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Split a comma-separated trusted-proxy allowlist from the environment."""
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def proxy_trust_enabled(settings: Settings) -> bool:
    """True when forwarding headers may be consulted for admin login sources."""
    return settings.admin_trust_proxy_headers and bool(
        parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    )


def trusted_proxy_boundary(settings: Settings) -> TrustedProxyBoundary:
    return TrustedProxyBoundary.from_cidrs(
        parse_trusted_proxy_cidrs(settings.admin_trusted_proxy_cidrs)
    )


def normalize_client_address(raw: str | None) -> str | None:
    """Return a canonical client address or ``None`` when the value is unusable."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        host_part = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder.startswith(":"):
            port = remainder[1:]
            if not port.isdigit():
                return None
        elif remainder:
            return None
        candidate = host_part
    elif candidate.count(":") == 1 and "." in candidate:
        host_part, port = candidate.split(":", 1)
        if not port.isdigit():
            return None
        candidate = host_part

    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _peer_identity(immediate_peer: str | None) -> str:
    normalized = normalize_client_address(immediate_peer)
    if normalized is not None:
        return normalized
    raw = (immediate_peer or "").strip()
    if not raw:
        return "unknown"
    return raw.lower()


def _split_forward_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _walk_trusted_xff_chain(
    chain: list[str],
    *,
    immediate_peer: str,
    boundary: TrustedProxyBoundary,
) -> ClientSourceResolution | None:
    if len(chain) > _MAX_FORWARD_CHAIN_LENGTH:
        return ClientSourceResolution("unknown", PATH_CONSERVATIVE_AMBIGUOUS)

    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return ClientSourceResolution("unknown", PATH_CONSERVATIVE_UNKNOWN)
        normalized_chain.append(normalized)

    peer = _peer_identity(immediate_peer)
    if peer == "unknown" and not immediate_peer.strip():
        return ClientSourceResolution("unknown", PATH_CONSERVATIVE_UNKNOWN)

    if normalized_chain:
        rightmost = normalized_chain[-1]
        if rightmost != peer:
            return ClientSourceResolution(peer, PATH_CONSERVATIVE_UNTRUSTED_HEADERS)

    for hop in reversed(normalized_chain):
        if not boundary.trusts(hop):
            return ClientSourceResolution(hop, PATH_XFF_TRUSTED_WALK)

    # Every hop is trusted — fail closed to the immediate peer bucket.
    return ClientSourceResolution(peer, PATH_CONSERVATIVE_AMBIGUOUS)


def _parse_forwarded_for_header(request: Request) -> str | None:
    value = request.headers.get("x-forwarded-for")
    if not value:
        return None
    return value.strip() or None


def _parse_forwarded_header(request: Request) -> str | None:
    value = request.headers.get("forwarded")
    if not value:
        return None
    for entry in value.split(","):
        match = _FORWARDED_FOR_TOKEN_RE.search(entry.strip())
        if match is None:
            continue
        candidate = match.group(1) or match.group(2)
        if candidate:
            return candidate.strip()
    return None


def _parse_cf_connecting_ip(request: Request) -> str | None:
    value = request.headers.get("cf-connecting-ip")
    if not value:
        return None
    return value.strip() or None


def _maybe_log_invalid_header_attempt(*, path: str, reason: str) -> None:
    global _invalid_header_event_count
    with _telemetry_lock:
        _invalid_header_event_count += 1
        sample = _invalid_header_event_count % _INVALID_HEADER_SAMPLE_RATE == 1
    if sample:
        _logger.warning(
            "Admin login source resolution rejected forwarding headers",
            extra={"resolution_path": path, "rejection_reason": reason},
        )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective admin-login client source for shared limiter buckets.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Client → Cloudflare edge → Render load balancer → Uvicorn → FastAPI``

    Forwarding headers are consulted only when the immediate TCP peer is a member
    of ``ADMIN_TRUSTED_PROXY_CIDRS``. The left-most ``X-Forwarded-For`` value is
    never trusted directly; hops are evaluated from right to left, matching
    Uvicorn's ``ProxyHeadersMiddleware`` semantics.
    """
    immediate_peer = request.client.host if request.client is not None else None
    peer = _peer_identity(immediate_peer)
    if peer == "unknown" and not (immediate_peer or "").strip():
        return ClientSourceResolution("unknown", PATH_CONSERVATIVE_UNKNOWN)

    boundary = trusted_proxy_boundary(settings)
    if not proxy_trust_enabled(settings) or not boundary.configured:
        return ClientSourceResolution(peer, PATH_DIRECT_PEER)

    if not boundary.trusts(peer):
        had_forwarding_headers = any(
            request.headers.get(name)
            for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
        )
        if had_forwarding_headers:
            _maybe_log_invalid_header_attempt(
                path=PATH_CONSERVATIVE_UNTRUSTED_HEADERS,
                reason="untrusted_immediate_peer",
            )
        return ClientSourceResolution(peer, PATH_DIRECT_PEER)

    xff_value = _parse_forwarded_for_header(request)
    if xff_value is not None:
        chain = _split_forward_chain(xff_value)
        if not chain:
            return ClientSourceResolution(peer, PATH_CONSERVATIVE_UNKNOWN)
        return _walk_trusted_xff_chain(chain, immediate_peer=peer, boundary=boundary)

    forwarded_value = _parse_forwarded_header(request)
    if forwarded_value is not None:
        normalized = normalize_client_address(forwarded_value)
        if normalized is None:
            return ClientSourceResolution("unknown", PATH_CONSERVATIVE_UNKNOWN)
        if boundary.trusts(normalized):
            return ClientSourceResolution(peer, PATH_CONSERVATIVE_AMBIGUOUS)
        return ClientSourceResolution(normalized, PATH_FORWARDED_HEADER)

    cf_value = _parse_cf_connecting_ip(request)
    if cf_value is not None:
        normalized = normalize_client_address(cf_value)
        if normalized is None:
            return ClientSourceResolution("unknown", PATH_CONSERVATIVE_UNKNOWN)
        if boundary.trusts(normalized):
            return ClientSourceResolution(peer, PATH_CONSERVATIVE_AMBIGUOUS)
        return ClientSourceResolution(normalized, PATH_CF_CONNECTING_IP)

    return ClientSourceResolution(peer, PATH_CONSERVATIVE_AMBIGUOUS)


def reset_proxy_trust_telemetry() -> None:
    """Clear sampled telemetry counters (tests only)."""
    global _invalid_header_event_count
    with _telemetry_lock:
        _invalid_header_event_count = 0
