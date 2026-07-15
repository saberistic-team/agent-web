"""Trusted-hop client source resolution for admin login rate limiting."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

_logger = logging.getLogger(__name__)

MAX_FORWARDING_CHAIN_LENGTH = 32
_TELEMETRY_SAMPLE_INTERVAL_SECONDS = 60.0
_TELEMETRY_MAX_PATHS = 16

# Render internal load balancers connect from RFC1918 / shared address space.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "127.0.0.1/32",
    "::1/128",
    "fc00::/7",
)

# Loopback only: production client IP trust is enforced in-app (see admin_client_source).
DEFAULT_RENDER_FORWARDED_ALLOW_IPS = "127.0.0.1,::1"

_RESOLUTION_PATH_DIRECT_PEER = "direct_peer"
_RESOLUTION_PATH_TRUSTED_CHAIN = "trusted_chain"
_RESOLUTION_PATH_TRUSTED_FORWARDED = "trusted_forwarded"
_RESOLUTION_PATH_TRUSTED_CF_CONNECTING_IP = "trusted_cf_connecting_ip"
_RESOLUTION_PATH_UNTRUSTED_FORWARDING_IGNORED = "untrusted_forwarding_ignored"
_RESOLUTION_PATH_INVALID_FORWARDING_IGNORED = "invalid_forwarding_ignored"
_RESOLUTION_PATH_MISSING_FORWARDING = "missing_forwarding"
_RESOLUTION_PATH_MISSING_PEER = "missing_peer"

_FORWARDED_FOR_RE = re.compile(r"for=(\"([^\"\\]|\\.)*\"|[^;,\s]+)", re.IGNORECASE)

_telemetry_lock = threading.Lock()
_telemetry_last_emit: dict[str, float] = {}
_telemetry_counts: dict[str, int] = {}


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved client source identity for limiter keying."""

    address: str
    path: str


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 (incl. mapped) or return a stable non-IP host token."""
    candidate = raw.strip()
    if not candidate:
        return None

    host = candidate
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing > 0:
            host = candidate[1:closing]
            remainder = candidate[closing + 1 :].lstrip()
            if remainder.startswith(":") and remainder[1:].isdigit():
                pass
    elif candidate.count(":") == 1 and candidate.rsplit(":", 1)[-1].isdigit():
        host = candidate.rsplit(":", 1)[0]

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower()
        return lowered or None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed).lower()


def parse_trusted_proxy_networks(
    values: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured CIDR strings into network objects."""
    networks: list[ipaddress._BaseNetwork] = []
    for value in values:
        token = value.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def is_trusted_proxy_address(
    address: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_client_address(address)
    if normalized is None:
        return False
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def _split_forwarding_chain(header_value: str) -> list[str]:
    return [part.strip() for part in header_value.split(",") if part.strip()]


def _resolve_from_chain(
    chain: list[str],
    *,
    peer: str,
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str | None:
    if len(chain) > MAX_FORWARDING_CHAIN_LENGTH:
        return None

    normalized_chain: list[str] = []
    for hop in chain:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return None
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            return None
        normalized_chain.append(normalized)

    peer_normalized = normalize_client_address(peer)
    if peer_normalized is None:
        return None

    candidates = normalized_chain + [peer_normalized]
    while len(candidates) > 1 and is_trusted_proxy_address(candidates[-1], trusted_networks):
        candidates.pop()

    if len(candidates) == 1 and is_trusted_proxy_address(candidates[0], trusted_networks):
        return None
    return candidates[-1]


def _parse_forwarded_header(header_value: str) -> list[str]:
    addresses: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(header_value):
        token = match.group(1).strip()
        if token.startswith('"') and token.endswith('"'):
            token = bytes(token[1:-1], "utf-8").decode("unicode_escape")
        if token.lower() == "unknown":
            continue
        if token.startswith("[") and "]" in token:
            token = token[1 : token.index("]")]
        elif token.count(":") > 1 and token.startswith("["):
            continue
        elif token.count(":") == 1 and not token.startswith("["):
            host, maybe_port = token.rsplit(":", 1)
            if maybe_port.isdigit():
                token = host
        addresses.append(token)
    return addresses


def _record_resolution_telemetry(path: str, *, invalid: bool = False) -> None:
    now = time.monotonic()
    bucket = "invalid_forwarding" if invalid else path
    with _telemetry_lock:
        _telemetry_counts[bucket] = _telemetry_counts.get(bucket, 0) + 1
        last_emit = _telemetry_last_emit.get(bucket, 0.0)
        if now - last_emit < _TELEMETRY_SAMPLE_INTERVAL_SECONDS:
            return
        _telemetry_last_emit[bucket] = now
        if len(_telemetry_counts) > _TELEMETRY_MAX_PATHS:
            _telemetry_counts.clear()
            _telemetry_last_emit.clear()
    _logger.info(
        "Admin login client source resolution",
        extra={
            "source_resolution_path": path,
            "invalid_forwarding": invalid,
            "sampled_events": _telemetry_counts.get(bucket, 1),
        },
    )


def reset_client_source_telemetry_for_tests() -> None:
    """Clear sampled telemetry counters (tests only)."""
    with _telemetry_lock:
        _telemetry_counts.clear()
        _telemetry_last_emit.clear()


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login limiter buckets."""
    trusted_networks = settings.admin_trusted_proxy_networks
    peer = request.client.host if request.client is not None else ""
    if settings.admin_login_test_peer_header:
        override = request.headers.get("x-test-immediate-peer")
        if override:
            peer = override.strip()
    peer_normalized = normalize_client_address(peer)

    if peer_normalized is None:
        _record_resolution_telemetry(_RESOLUTION_PATH_MISSING_PEER)
        return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_MISSING_PEER)

    forwarding_present = any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    )

    if not trusted_networks:
        if forwarding_present:
            _record_resolution_telemetry(
                _RESOLUTION_PATH_UNTRUSTED_FORWARDING_IGNORED,
                invalid=True,
            )
        return ClientSourceResolution(
            address=peer_normalized,
            path=_RESOLUTION_PATH_DIRECT_PEER,
        )

    if not is_trusted_proxy_address(peer_normalized, trusted_networks):
        if forwarding_present:
            _record_resolution_telemetry(
                _RESOLUTION_PATH_UNTRUSTED_FORWARDING_IGNORED,
                invalid=True,
            )
        return ClientSourceResolution(
            address=peer_normalized,
            path=_RESOLUTION_PATH_DIRECT_PEER,
        )

    xff_raw = request.headers.get("x-forwarded-for", "")
    if xff_raw:
        chain = _split_forwarding_chain(xff_raw)
        resolved = _resolve_from_chain(chain, peer=peer_normalized, trusted_networks=trusted_networks)
        if resolved is not None:
            _record_resolution_telemetry(_RESOLUTION_PATH_TRUSTED_CHAIN)
            return ClientSourceResolution(address=resolved, path=_RESOLUTION_PATH_TRUSTED_CHAIN)
        _record_resolution_telemetry(
            _RESOLUTION_PATH_INVALID_FORWARDING_IGNORED,
            invalid=True,
        )
        return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_INVALID_FORWARDING_IGNORED)

    forwarded_raw = request.headers.get("forwarded", "")
    if forwarded_raw:
        chain = _parse_forwarded_header(forwarded_raw)
        if chain and len(chain) <= MAX_FORWARDING_CHAIN_LENGTH:
            resolved = _resolve_from_chain(chain, peer=peer_normalized, trusted_networks=trusted_networks)
            if resolved is not None:
                _record_resolution_telemetry(_RESOLUTION_PATH_TRUSTED_FORWARDED)
                return ClientSourceResolution(
                    address=resolved,
                    path=_RESOLUTION_PATH_TRUSTED_FORWARDED,
                )
        _record_resolution_telemetry(
            _RESOLUTION_PATH_INVALID_FORWARDING_IGNORED,
            invalid=True,
        )
        return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_INVALID_FORWARDING_IGNORED)

    cf_raw = request.headers.get("cf-connecting-ip", "")
    if cf_raw:
        cf_normalized = normalize_client_address(cf_raw.strip())
        if cf_normalized is not None and not is_trusted_proxy_address(
            cf_normalized,
            trusted_networks,
        ):
            _record_resolution_telemetry(_RESOLUTION_PATH_TRUSTED_CF_CONNECTING_IP)
            return ClientSourceResolution(
                address=cf_normalized,
                path=_RESOLUTION_PATH_TRUSTED_CF_CONNECTING_IP,
            )
        _record_resolution_telemetry(
            _RESOLUTION_PATH_INVALID_FORWARDING_IGNORED,
            invalid=True,
        )
        return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_INVALID_FORWARDING_IGNORED)

    if forwarding_present:
        _record_resolution_telemetry(
            _RESOLUTION_PATH_INVALID_FORWARDING_IGNORED,
            invalid=True,
        )
        return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_INVALID_FORWARDING_IGNORED)

    _record_resolution_telemetry(_RESOLUTION_PATH_MISSING_FORWARDING)
    return ClientSourceResolution(address="unknown", path=_RESOLUTION_PATH_MISSING_FORWARDING)
