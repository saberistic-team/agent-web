"""Trusted-proxy boundary helpers for admin login client-source resolution."""

from __future__ import annotations

import functools
import ipaddress
import re
from dataclasses import dataclass

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/).
CLOUDFLARE_IPV4_CIDRS: tuple[str, ...] = (
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "108.162.192.0/18",
    "131.0.72.0/22",
    "141.101.64.0/18",
    "162.158.0.0/15",
    "172.64.0.0/13",
    "173.245.48.0/20",
    "188.114.96.0/20",
    "190.93.240.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
)

CLOUDFLARE_IPV6_CIDRS: tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Guard against unbounded header parsing work.
MAX_FORWARDED_CHAIN_LENGTH = 32

_FORWARDED_FOR_TOKEN = re.compile(
    r'(?:^|;)\s*for=(?:"(?P<quoted>[^"\\]*(?:\\.[^"\\]*)*)"|(?P<unquoted>[^;,\s]+))',
    re.IGNORECASE,
)


def parse_host_list(raw_value: str) -> list[str]:
    """Split a comma-separated forwarded header value into trimmed tokens."""
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def parse_host_port(value: str) -> tuple[str, int]:
    """Parse a host token that may include an optional port."""
    if value.startswith("["):
        bracket_end = value.find("]")
        if bracket_end == -1:
            return value, 0
        host = value[1:bracket_end]
        remainder = value[bracket_end + 1 :]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return value, 0
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0

    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0

    return value, 0


def normalize_ip_address(host: str) -> str | None:
    """Return a canonical IP string or ``None`` when the token is not an IP address."""
    candidate = host.strip().lower()
    if not candidate:
        return None
    if candidate.startswith("_"):
        return None
    host_only, _port = parse_host_port(candidate)
    try:
        address = ipaddress.ip_address(host_only)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


@dataclass(frozen=True)
class TrustedProxyBoundary:
    """Membership checks for immediate peers and forwarding-chain hops."""

    immediate_peer_cidrs: tuple[str, ...]
    forwarding_chain_cidrs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_immediate", _TrustedHosts(self.immediate_peer_cidrs))
        object.__setattr__(self, "_forwarding", _TrustedHosts(self.forwarding_chain_cidrs))

    def immediate_peer_trusted(self, host: str | None) -> bool:
        return host is not None and host in self._immediate

    def forwarding_hop_trusted(self, host: str | None) -> bool:
        return host is not None and host in self._forwarding

    def cloudflare_hop_present(self, hosts: list[str]) -> bool:
        cloudflare = _TrustedHosts(CLOUDFLARE_IPV4_CIDRS + CLOUDFLARE_IPV6_CIDRS)
        for host in hosts:
            if host in cloudflare:
                return True
        return False

    def client_from_forwarded_for(self, x_forwarded_for: str) -> str | None:
        """Return the first untrusted hop scanning X-Forwarded-For right-to-left."""
        host_ports = parse_host_list(x_forwarded_for)
        if len(host_ports) > MAX_FORWARDED_CHAIN_LENGTH:
            return None
        if not host_ports:
            return None

        for host_port in reversed(host_ports):
            host, _port = parse_host_port(host_port)
            normalized = normalize_ip_address(host)
            if normalized is None:
                return None
            if normalized not in self._forwarding:
                return normalized

        first_host, _ = parse_host_port(host_ports[0])
        normalized_first = normalize_ip_address(first_host)
        return normalized_first

    def client_from_forwarded_header(self, forwarded_header: str) -> str | None:
        """Parse RFC 7239 ``Forwarded`` and return the leftmost untrusted ``for`` hop."""
        for_values: list[str] = []
        for segment in forwarded_header.split(","):
            for match in _FORWARDED_FOR_TOKEN.finditer(segment):
                raw = match.group("quoted") or match.group("unquoted") or ""
                token = raw.strip()
                if token.lower().startswith("unknown"):
                    continue
                if token.startswith("[") and "]" in token:
                    token = token[1 : token.index("]")]
                elif token.count(":") > 1 and not token.startswith("["):
                    token = token.split(":", 1)[0]
                if token:
                    for_values.append(token)

        if len(for_values) > MAX_FORWARDED_CHAIN_LENGTH:
            return None
        if not for_values:
            return None

        for candidate in reversed(for_values):
            normalized = normalize_ip_address(candidate)
            if normalized is None:
                return None
            if normalized not in self._forwarding:
                return normalized

        normalized_first = normalize_ip_address(for_values[0])
        return normalized_first


class _TrustedHosts:
    """Trusted host/network membership (mirrors Uvicorn semantics)."""

    def __init__(self, trusted_hosts: tuple[str, ...] | list[str]) -> None:
        self.always_trust = trusted_hosts in ("*", ["*"])
        self.trusted_literals: set[str] = set()
        self.trusted_hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        self.trusted_networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()

        if not self.always_trust:
            for host in trusted_hosts:
                if "/" in host:
                    try:
                        self.trusted_networks.add(ipaddress.ip_network(host, strict=False))
                    except ValueError:
                        self.trusted_literals.add(host)
                else:
                    try:
                        self.trusted_hosts.add(ipaddress.ip_address(host))
                    except ValueError:
                        self.trusted_literals.add(host)

        self._trusts = functools.lru_cache(maxsize=4096)(self._compute_trust)

    def __contains__(self, host: str | None) -> bool:
        if self.always_trust:
            return True
        if not host:
            return False
        if len(host) > 253:
            return self._compute_trust(host)
        return self._trusts(host)

    def _compute_trust(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host in self.trusted_literals
        if ip in self.trusted_hosts:
            return True
        return any(ip in net for net in self.trusted_networks)
