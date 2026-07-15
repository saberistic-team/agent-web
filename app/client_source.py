"""Trusted-proxy client source resolution for admin login rate limiting."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request

from app.config import Settings

# Telemetry path identifiers (no raw addresses in logs).
PATH_DIRECT_PEER = "direct_peer"
PATH_MISSING_PEER = "missing_peer"
PATH_UNTRUSTED_FORWARDED = "untrusted_forwarded"
PATH_TRUSTED_CHAIN = "trusted_chain"
PATH_TRUSTED_PEER_FALLBACK = "trusted_peer_fallback"
PATH_CF_CONNECTING_IP = "cf_connecting_ip"
PATH_RFC_FORWARDED = "rfc_forwarded"
PATH_MALFORMED_FORWARDED = "malformed_forwarded"

MAX_FORWARDED_CHAIN_LENGTH = 32
_UNTRUSTED_FORWARDED_SAMPLE_RATE = 100

_logger = logging.getLogger(__name__)
_untrusted_forwarded_counter = 0
_untrusted_forwarded_lock = threading.Lock()

_FORWARDED_FOR_TOKEN_RE = re.compile(
    r"^\s*for=(?:\"([^\"]+)\"|([^;,\s]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClientSourceResolution:
    """Resolved limiter source identity and the resolution path used."""

    source: str
    path: str


class TrustedProxyBoundary:
    """Trusted immediate peers allowed to supply forwarding headers."""

    def __init__(self, trusted_hosts: str | Iterable[str]) -> None:
        if isinstance(trusted_hosts, str):
            trusted_hosts_list = trusted_hosts
        else:
            trusted_hosts_list = list(trusted_hosts)

        self.always_trust = trusted_hosts_list in ("*", ["*"])

        self.trusted_literals: set[str] = set()
        self.trusted_hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.trusted_networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

        if not self.always_trust:
            if isinstance(trusted_hosts, str):
                entries = _parse_raw_hosts(trusted_hosts)
            else:
                entries = [str(item).strip() for item in trusted_hosts_list if str(item).strip()]

            for host in entries:
                if "/" in host:
                    try:
                        self.trusted_networks.add(ipaddress.ip_network(host))
                    except ValueError:
                        self.trusted_literals.add(host)
                else:
                    try:
                        self.trusted_hosts.add(ipaddress.ip_address(host))
                    except ValueError:
                        self.trusted_literals.add(host)

        self._trusts = functools.lru_cache(maxsize=4096)(self._compute_trust)

    def __contains__(self, host: str) -> bool:
        if self.always_trust:
            return True
        return self._trusts(host)

    def _compute_trust(self, host: str) -> bool:
        if host in self.trusted_literals:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip in self.trusted_hosts:
            return True
        return any(ip in network for network in self.trusted_networks)

    def client_from_x_forwarded_for(self, x_forwarded_for: str) -> str | None:
        """Return the first untrusted hop walking X-Forwarded-For right-to-left."""
        hosts = _parse_forwarded_chain(x_forwarded_for)
        if not hosts:
            return None
        if len(hosts) > MAX_FORWARDED_CHAIN_LENGTH:
            return None

        if self.always_trust:
            first = _normalize_client_address(hosts[0])
            return first

        for host_port in reversed(hosts):
            host, _port = _parse_host_port(host_port)
            if host not in self:
                normalized = _normalize_client_address(host)
                if normalized is not None:
                    return normalized
                return None

        normalized = _normalize_client_address(hosts[0])
        return normalized


def resolve_admin_login_client_source(
    request: Request,
    settings: Settings,
) -> ClientSourceResolution:
    """Resolve the effective client source for admin login rate limiting."""
    peer_host, peer_resolution = _immediate_peer(request)
    if not settings.admin_trust_proxy_headers:
        return ClientSourceResolution(source=peer_resolution.source, path=peer_resolution.path)

    boundary = TrustedProxyBoundary(settings.admin_trusted_proxy_ips)
    if peer_host is None:
        return ClientSourceResolution(source="unknown", path=PATH_MISSING_PEER)

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    if x_forwarded_for:
        chain_hosts = _parse_forwarded_chain(x_forwarded_for)
        if len(chain_hosts) > MAX_FORWARDED_CHAIN_LENGTH:
            _record_untrusted_forwarded(PATH_MALFORMED_FORWARDED)
            return ClientSourceResolution(source="unknown", path=PATH_MALFORMED_FORWARDED)

        if len(chain_hosts) >= 2:
            rightmost_raw = chain_hosts[-1]
            rightmost_host, _ = _parse_host_port(rightmost_raw)
            if not _hop_is_trusted_proxy(rightmost_host, rightmost_raw, boundary):
                _record_untrusted_forwarded(PATH_UNTRUSTED_FORWARDED)
                return ClientSourceResolution(source="unknown", path=PATH_UNTRUSTED_FORWARDED)

            client = boundary.client_from_x_forwarded_for(x_forwarded_for)
            if client is None:
                _record_untrusted_forwarded(PATH_MALFORMED_FORWARDED)
                return ClientSourceResolution(source="unknown", path=PATH_MALFORMED_FORWARDED)

            peer_matches_proxy = _peer_matches_rightmost_hop(
                peer_host,
                rightmost_host,
                rightmost_raw,
            )
            uvicorn_already_resolved = peer_host == client and peer_host not in boundary
            if not peer_matches_proxy and not uvicorn_already_resolved:
                _record_untrusted_forwarded(PATH_UNTRUSTED_FORWARDED)
                return ClientSourceResolution(source="unknown", path=PATH_UNTRUSTED_FORWARDED)

            cf_ip = _normalize_client_address(request.headers.get("cf-connecting-ip", ""))
            if cf_ip is not None and cf_ip == client:
                return ClientSourceResolution(source=client, path=PATH_CF_CONNECTING_IP)
            return ClientSourceResolution(source=client, path=PATH_TRUSTED_CHAIN)

        if len(chain_hosts) == 1:
            _record_untrusted_forwarded(PATH_TRUSTED_PEER_FALLBACK)
            return ClientSourceResolution(source="unknown", path=PATH_TRUSTED_PEER_FALLBACK)

    if peer_host not in boundary:
        _record_untrusted_forwarded(PATH_UNTRUSTED_FORWARDED)
        return ClientSourceResolution(source="unknown", path=PATH_UNTRUSTED_FORWARDED)

    forwarded_header = request.headers.get("forwarded", "")
    if forwarded_header:
        client = _client_from_forwarded_header(forwarded_header)
        if client is not None:
            return ClientSourceResolution(source=client, path=PATH_RFC_FORWARDED)
        _record_untrusted_forwarded(PATH_MALFORMED_FORWARDED)
        return ClientSourceResolution(source="unknown", path=PATH_MALFORMED_FORWARDED)

    cf_ip = _normalize_client_address(request.headers.get("cf-connecting-ip", ""))
    if cf_ip is not None:
        _record_untrusted_forwarded(PATH_UNTRUSTED_FORWARDED)
        return ClientSourceResolution(source="unknown", path=PATH_UNTRUSTED_FORWARDED)

    return ClientSourceResolution(source=peer_host, path=PATH_DIRECT_PEER)


def _hop_is_trusted_proxy(
    host: str,
    raw: str,
    boundary: TrustedProxyBoundary,
) -> bool:
    if host in boundary or raw.strip().lower() in boundary.trusted_literals:
        return True
    normalized = _normalize_client_address(host)
    return normalized is not None and normalized in boundary


def _peer_matches_rightmost_hop(peer_host: str, rightmost_host: str, rightmost_raw: str) -> bool:
    if peer_host == rightmost_host:
        return True
    if peer_host == rightmost_raw.strip().lower():
        return True
    peer_normalized = _normalize_client_address(peer_host)
    rightmost_normalized = _normalize_client_address(rightmost_host)
    return (
        peer_normalized is not None
        and rightmost_normalized is not None
        and peer_normalized == rightmost_normalized
    )


def _immediate_peer(request: Request) -> tuple[str | None, ClientSourceResolution]:
    if request.client is None:
        return None, ClientSourceResolution(source="unknown", path=PATH_MISSING_PEER)
    host, _port = _parse_host_port(request.client.host)
    normalized = _normalize_client_address(host)
    if normalized is None:
        literal = host.strip().lower()
        if literal:
            return literal, ClientSourceResolution(source=literal, path=PATH_DIRECT_PEER)
        return None, ClientSourceResolution(source="unknown", path=PATH_MISSING_PEER)
    return normalized, ClientSourceResolution(source=normalized, path=PATH_DIRECT_PEER)


def _parse_raw_hosts(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_forwarded_chain(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_host_port(host_port: str) -> tuple[str, int]:
    host_port = host_port.strip()
    if not host_port:
        return "", 0
    if host_port.startswith("["):
        end = host_port.find("]")
        if end == -1:
            return host_port, 0
        host = host_port[1:end]
        port = 0
        if end + 1 < len(host_port) and host_port[end + 1] == ":":
            try:
                port = int(host_port[end + 2 :])
            except ValueError:
                port = 0
        return host, port
    if host_port.count(":") == 1:
        host, _, port_text = host_port.partition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return host, 0
    return host_port, 0


def _normalize_client_address(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    host, _port = _parse_host_port(candidate)
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _client_from_forwarded_header(value: str) -> str | None:
    for entry in _parse_forwarded_chain(value):
        match = _FORWARDED_FOR_TOKEN_RE.match(entry)
        if not match:
            continue
        raw = match.group(1) or match.group(2) or ""
        normalized = _normalize_client_address(raw)
        if normalized is not None:
            return normalized
    return None


def _record_untrusted_forwarded(path: str) -> None:
    global _untrusted_forwarded_counter
    with _untrusted_forwarded_lock:
        _untrusted_forwarded_counter += 1
        should_log = _untrusted_forwarded_counter % _UNTRUSTED_FORWARDED_SAMPLE_RATE == 1
    if should_log:
        _logger.info(
            "Admin login client source forwarding rejected",
            extra={"resolution_path": path},
        )


def reset_untrusted_forwarded_telemetry() -> None:
    """Reset sampled telemetry counters (tests only)."""
    global _untrusted_forwarded_counter
    with _untrusted_forwarded_lock:
        _untrusted_forwarded_counter = 0
