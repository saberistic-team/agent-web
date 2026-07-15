"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDED_CHAIN_LENGTH: Final[int] = 10
_DEFAULT_RENDER_TRUSTED_CIDRS: Final[tuple[str, ...]] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "100.64.0.0/10",
)

_TELEMETRY_SAMPLE_WINDOW_SECONDS = 60
_TELEMETRY_SAMPLE_LIMIT = 20
_telemetry_window_start = 0.0
_telemetry_sample_count = 0

_FORWARDED_FOR_TOKEN = re.compile(
    r"""^for=
        (?:
            "(?P<quoted>[^"]+)"
            |
            (?P<unquoted>[^;,\s"]+)
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class SourceResolutionPath:
    """Bounded telemetry codes; never include raw addresses."""

    DIRECT_PEER = "direct_peer"
    TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
    XFF_TRUSTED_CHAIN = "xff_trusted_chain"
    FORWARDED_TRUSTED_CHAIN = "forwarded_trusted_chain"
    CF_CONNECTING_IP = "cf_connecting_ip"
    PEER_MISSING = "peer_missing"
    HEADER_UNTRUSTED = "header_untrusted"
    HEADER_MALFORMED = "header_malformed"
    CHAIN_EXCEEDED = "chain_exceeded"


@dataclass(frozen=True)
class SourceResolutionResult:
    source: str
    path: str


class TrustedProxyNetworks:
    """Configured trusted proxy CIDRs for hop verification."""

    def __init__(self, cidrs: tuple[str, ...]) -> None:
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for cidr in cidrs:
            entry = cidr.strip()
            if not entry:
                continue
            try:
                self._networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue

    @property
    def configured(self) -> bool:
        return bool(self._networks)

    def contains(self, address: str) -> bool:
        normalized = normalize_client_address(address)
        if normalized is None:
            return False
        try:
            ip = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        return any(ip in network for network in self._networks)


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; strip ports and zones."""
    candidate = raw.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate.partition("]")
        candidate = host[1:]
        if remainder.startswith(":") and remainder[1:].isdigit():
            pass
    elif candidate.count(":") == 1 and candidate.rsplit(":", 1)[-1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]

    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def parse_x_forwarded_for(header_value: str) -> list[str]:
    """Return left-to-right X-Forwarded-For hops with normalized addresses."""
    if not header_value or len(header_value) > 4096:
        return []

    hops: list[str] = []
    for part in header_value.split(","):
        normalized = normalize_client_address(part)
        if normalized is None:
            continue
        hops.append(normalized)
        if len(hops) >= MAX_FORWARDED_CHAIN_LENGTH:
            break
    return hops


def parse_forwarded_header(header_value: str) -> list[str]:
    """Return left-to-right client addresses from an RFC 7239 Forwarded header."""
    if not header_value or len(header_value) > 4096:
        return []

    hops: list[str] = []
    for entry in header_value.split(","):
        match = _FORWARDED_FOR_TOKEN.search(entry.strip())
        if match is None:
            continue
        raw_value = match.group("quoted") or match.group("unquoted") or ""
        normalized = normalize_client_address(raw_value)
        if normalized is None:
            continue
        hops.append(normalized)
        if len(hops) >= MAX_FORWARDED_CHAIN_LENGTH:
            break
    return hops


def _peer_host(request: Request) -> str | None:
    if request.client is None:
        return None
    host = request.client.host
    if not host:
        return None
    return host.strip() or None


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )


def _trusted_proxy_networks(settings: Settings) -> TrustedProxyNetworks | None:
    cidrs = settings.admin_trusted_proxy_cidrs
    if not cidrs and settings.admin_trust_proxy_headers:
        cidrs = _DEFAULT_RENDER_TRUSTED_CIDRS
    if not cidrs:
        return None
    networks = TrustedProxyNetworks(cidrs)
    if not networks.configured:
        return None
    return networks


def _cloudflare_proxy_networks(settings: Settings) -> TrustedProxyNetworks | None:
    cidrs = settings.admin_cloudflare_proxy_cidrs
    if not cidrs:
        return None
    networks = TrustedProxyNetworks(cidrs)
    if not networks.configured:
        return None
    return networks


def _walk_trusted_chain(
    hops_right_to_left: list[str],
    trusted: TrustedProxyNetworks,
) -> str | None:
    """Return the first untrusted hop closest to the server, if any."""
    for hop in hops_right_to_left:
        if trusted.contains(hop):
            continue
        return hop
    return None


def _chain_hops_right_to_left(peer: str, header_hops_left_to_right: list[str]) -> list[str]:
    return [peer, *reversed(header_hops_left_to_right)]


def _cloudflare_hop_present(
    hops: list[str],
    *,
    cloudflare: TrustedProxyNetworks,
) -> bool:
    return any(cloudflare.contains(hop) for hop in hops)


def _log_resolution(path: str) -> None:
    _logger.info(
        "Admin login client source resolved",
        extra={"source_resolution_path": path},
    )


def _log_sampled(path: str) -> None:
    global _telemetry_window_start, _telemetry_sample_count
    now = time.monotonic()
    if now - _telemetry_window_start >= _TELEMETRY_SAMPLE_WINDOW_SECONDS:
        _telemetry_window_start = now
        _telemetry_sample_count = 0
    if _telemetry_sample_count >= _TELEMETRY_SAMPLE_LIMIT:
        return
    _telemetry_sample_count += 1
    _logger.info(
        "Admin login forwarding header rejected",
        extra={"source_resolution_path": path},
    )


def reset_source_resolution_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _telemetry_window_start, _telemetry_sample_count
    _telemetry_window_start = 0.0
    _telemetry_sample_count = 0


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> SourceResolutionResult:
    """Resolve the effective client source for admin login rate limiting.

    Forwarding headers are consulted only when the immediate peer is a member of
    ``ADMIN_TRUSTED_PROXY_CIDRS``. The right-most untrusted hop in the chain is
    used; attacker-controlled left-most values are never selected directly.
    """
    peer = _peer_host(request)
    if peer is None:
        _log_resolution(SourceResolutionPath.PEER_MISSING)
        return SourceResolutionResult("unknown", SourceResolutionPath.PEER_MISSING)

    peer_normalized = normalize_client_address(peer)
    effective_peer = peer_normalized if peer_normalized is not None else peer

    trusted = _trusted_proxy_networks(settings)
    if trusted is None:
        return SourceResolutionResult(effective_peer, SourceResolutionPath.DIRECT_PEER)

    if peer_normalized is None or not trusted.contains(peer_normalized):
        if _has_forwarding_headers(request):
            _log_sampled(SourceResolutionPath.HEADER_UNTRUSTED)
        return SourceResolutionResult(effective_peer, SourceResolutionPath.DIRECT_PEER)

    peer_identity = peer_normalized

    xff_header = request.headers.get("x-forwarded-for", "")
    if xff_header:
        if len(xff_header) > 4096:
            _log_sampled(SourceResolutionPath.CHAIN_EXCEEDED)
            return SourceResolutionResult(
                peer_identity,
                SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            )
        xff_hops = parse_x_forwarded_for(xff_header)
        if len(xff_hops) >= MAX_FORWARDED_CHAIN_LENGTH:
            _log_sampled(SourceResolutionPath.CHAIN_EXCEEDED)
            return SourceResolutionResult(
                peer_identity,
                SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            )
        client = _walk_trusted_chain(
            _chain_hops_right_to_left(peer_identity, xff_hops),
            trusted,
        )
        if client is not None:
            _log_resolution(SourceResolutionPath.XFF_TRUSTED_CHAIN)
            return SourceResolutionResult(client, SourceResolutionPath.XFF_TRUSTED_CHAIN)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        if len(forwarded_header) > 4096:
            _log_sampled(SourceResolutionPath.CHAIN_EXCEEDED)
            return SourceResolutionResult(
                peer_identity,
                SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            )
        forwarded_hops = parse_forwarded_header(forwarded_header)
        if len(forwarded_hops) >= MAX_FORWARDED_CHAIN_LENGTH:
            _log_sampled(SourceResolutionPath.CHAIN_EXCEEDED)
            return SourceResolutionResult(
                peer_identity,
                SourceResolutionPath.TRUSTED_PEER_FALLBACK,
            )
        client = _walk_trusted_chain(
            _chain_hops_right_to_left(peer_identity, forwarded_hops),
            trusted,
        )
        if client is not None:
            _log_resolution(SourceResolutionPath.FORWARDED_TRUSTED_CHAIN)
            return SourceResolutionResult(client, SourceResolutionPath.FORWARDED_TRUSTED_CHAIN)

    if settings.admin_trust_cloudflare_connecting_ip:
        cloudflare = _cloudflare_proxy_networks(settings)
        cf_header = request.headers.get("cf-connecting-ip", "")
        if cloudflare is not None and cf_header:
            cf_client = normalize_client_address(cf_header)
            if cf_client is None:
                _log_sampled(SourceResolutionPath.HEADER_MALFORMED)
            else:
                chain_hops = [peer_identity]
                if xff_header:
                    chain_hops.extend(parse_x_forwarded_for(xff_header))
                if _cloudflare_hop_present(chain_hops, cloudflare=cloudflare):
                    _log_resolution(SourceResolutionPath.CF_CONNECTING_IP)
                    return SourceResolutionResult(
                        cf_client,
                        SourceResolutionPath.CF_CONNECTING_IP,
                    )
                _log_sampled(SourceResolutionPath.HEADER_UNTRUSTED)

    _log_resolution(SourceResolutionPath.TRUSTED_PEER_FALLBACK)
    return SourceResolutionResult(peer_identity, SourceResolutionPath.TRUSTED_PEER_FALLBACK)
