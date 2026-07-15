"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import random
import re
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Conservative cap for forwarded chains; excess hops are treated as malformed.
_MAX_FORWARDED_CHAIN_LENGTH = 32

# Sampled telemetry for invalid/untrusted forwarding attempts (no raw addresses).
_INVALID_FORWARDING_SAMPLE_RATE = 0.05

_logger = logging.getLogger(__name__)

# Resolution paths surfaced in bounded structured telemetry (no raw IPs).
PATH_DIRECT_PEER = "direct_peer"
PATH_UNTRUSTED_PEER = "untrusted_peer"
PATH_TRUSTED_XFF = "trusted_xff"
PATH_TRUSTED_FORWARDED = "trusted_forwarded"
PATH_MALFORMED_FORWARDING = "malformed_forwarding"
PATH_MISSING_PEER = "missing_peer"

_FORWARDED_FOR_TOKEN_RE = re.compile(r"for=(?:" r'"([^"]+)"' r"|'([^']+)'|([^;,]+))", re.IGNORECASE)


@dataclass(frozen=True)
class AdminClientSourceResult:
    """Resolved limiter source identity and the resolution path used."""

    source: str
    path: str
    hop_count: int = 0


@dataclass(frozen=True)
class _TrustedProxyBoundary:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @classmethod
    def from_entries(cls, entries: Iterable[str]) -> _TrustedProxyBoundary:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry:
                continue
            try:
                if "/" in entry:
                    networks.append(ipaddress.ip_network(entry, strict=False))
                else:
                    parsed = ipaddress.ip_address(entry)
                    prefix = 32 if parsed.version == 4 else 128
                    networks.append(ipaddress.ip_network(f"{parsed}/{prefix}", strict=False))
            except ValueError:
                continue
        return cls(networks=tuple(networks))

    def contains(self, address: str) -> bool:
        if not address or not self.networks:
            return False
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in self.networks)


def _strip_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("[") and "]" in value:
        host, _, remainder = value.partition("]")
        host = host.lstrip("[")
        if remainder.startswith(":") and remainder[1:].isdigit():
            return host
        return host
    if value.count(":") == 1 and value.rsplit(":", 1)[-1].isdigit():
        return value.rsplit(":", 1)[0]
    return value


def normalize_ip_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; return None when invalid."""
    candidate = _strip_port(raw.strip())
    if not candidate:
        return None
    if candidate.lower() == "unknown":
        return "unknown"
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if parsed.version == 6:
        return parsed.compressed
    return str(parsed)


def _parse_x_forwarded_for(header_value: str) -> list[str]:
    hops: list[str] = []
    for token in header_value.split(","):
        normalized = normalize_ip_address(token)
        if normalized is None:
            return []
        hops.append(normalized)
    return hops


def _parse_forwarded_header(header_value: str) -> list[str]:
    hops: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN_RE.search(entry)
        if match is None:
            return []
        raw_for = match.group(1) or match.group(2) or match.group(3) or ""
        raw_for = raw_for.removeprefix("for=").strip()
        if raw_for.lower() == "unknown":
            continue
        normalized = normalize_ip_address(raw_for)
        if normalized is None:
            return []
        hops.append(normalized)
    return hops


def _append_peer_hop(hops: list[str], peer: str | None) -> list[str]:
    if not peer:
        return hops
    if hops and hops[-1] == peer:
        return hops
    return [*hops, peer]


def _resolve_from_trusted_chain(
    hops: list[str],
    *,
    trusted_boundary: _TrustedProxyBoundary,
) -> str | None:
    if not hops:
        return None
    if len(hops) > _MAX_FORWARDED_CHAIN_LENGTH:
        return None
    for hop in reversed(hops):
        if not trusted_boundary.contains(hop):
            return hop
    return hops[0]


def _immediate_peer(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host.strip()
    if not host:
        return None
    normalized = normalize_ip_address(host)
    if normalized is not None:
        return normalized
    return host.lower()


def _log_resolution(result: AdminClientSourceResult) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={
            "source_resolution_path": result.path,
            "forwarding_hop_count": result.hop_count,
        },
    )


def _log_invalid_forwarding(path: str, *, hop_count: int) -> None:
    if random.random() >= _INVALID_FORWARDING_SAMPLE_RATE:
        return
    _logger.warning(
        "Admin login forwarding headers rejected",
        extra={
            "source_resolution_path": path,
            "forwarding_hop_count": hop_count,
        },
    )


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> AdminClientSourceResult:
    """Resolve the effective client source for admin login rate limiting.

    Production chain (documented in ``docs/ADMIN_AUTH.md``):

    ``Client → Cloudflare → Render load balancer → Uvicorn``

    Forwarding headers are honored only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_IPS``. The client address is derived by walking the
    forwarding chain from right to left and selecting the first hop that is not
    a configured trusted proxy. Vendor-specific headers such as
    ``CF-Connecting-IP`` are accepted only after a verified Cloudflare hop is
    present in the chain.
    """
    peer = _immediate_peer(request)
    if peer is None:
        result = AdminClientSourceResult(source="unknown", path=PATH_MISSING_PEER)
        _log_resolution(result)
        return result

    if not settings.admin_trust_proxy_headers:
        result = AdminClientSourceResult(source=peer, path=PATH_DIRECT_PEER)
        _log_resolution(result)
        return result

    trusted_boundary = _TrustedProxyBoundary.from_entries(settings.admin_trusted_proxy_ips)
    if not trusted_boundary.contains(peer):
        result = AdminClientSourceResult(source=peer, path=PATH_UNTRUSTED_PEER)
        _log_resolution(result)
        return result

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        xff_hops = _parse_x_forwarded_for(xff_header)
        if not xff_hops:
            _log_invalid_forwarding(PATH_MALFORMED_FORWARDING, hop_count=0)
            result = AdminClientSourceResult(
                source=peer,
                path=PATH_MALFORMED_FORWARDING,
                hop_count=0,
            )
            _log_resolution(result)
            return result
        chain = _append_peer_hop(xff_hops, peer)
        resolved = _resolve_from_trusted_chain(chain, trusted_boundary=trusted_boundary)
        if resolved is None:
            _log_invalid_forwarding(PATH_MALFORMED_FORWARDING, hop_count=len(chain))
            result = AdminClientSourceResult(
                source=peer,
                path=PATH_MALFORMED_FORWARDING,
                hop_count=len(chain),
            )
            _log_resolution(result)
            return result
        result = AdminClientSourceResult(
            source=resolved,
            path=PATH_TRUSTED_XFF,
            hop_count=len(chain),
        )
        _log_resolution(result)
        return result

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        forwarded_hops = _parse_forwarded_header(forwarded_header)
        if not forwarded_hops:
            _log_invalid_forwarding(PATH_MALFORMED_FORWARDING, hop_count=0)
            result = AdminClientSourceResult(
                source=peer,
                path=PATH_MALFORMED_FORWARDING,
                hop_count=0,
            )
            _log_resolution(result)
            return result
        chain = _append_peer_hop(forwarded_hops, peer)
        resolved = _resolve_from_trusted_chain(chain, trusted_boundary=trusted_boundary)
        if resolved is None:
            _log_invalid_forwarding(PATH_MALFORMED_FORWARDING, hop_count=len(chain))
            result = AdminClientSourceResult(
                source=peer,
                path=PATH_MALFORMED_FORWARDING,
                hop_count=len(chain),
            )
            _log_resolution(result)
            return result
        result = AdminClientSourceResult(
            source=resolved,
            path=PATH_TRUSTED_FORWARDED,
            hop_count=len(chain),
        )
        _log_resolution(result)
        return result

    cf_header = request.headers.get("cf-connecting-ip", "")
    if cf_header:
        # Vendor headers are never trusted without a verified Cloudflare hop in a
        # forwarding chain. Direct origin requests that spoof CF-Connecting-IP fall
        # back to the immediate peer (Render proxy), not the header value.
        _log_invalid_forwarding(PATH_UNTRUSTED_PEER, hop_count=0)
        result = AdminClientSourceResult(source=peer, path=PATH_UNTRUSTED_PEER)
        _log_resolution(result)
        return result

    result = AdminClientSourceResult(source=peer, path=PATH_DIRECT_PEER, hop_count=1)
    _log_resolution(result)
    return result


def client_ip(request: Request, settings: Settings) -> str:
    """Return the resolved client source string for admin login rate limiting."""
    return resolve_admin_login_client_source(request, settings).source
