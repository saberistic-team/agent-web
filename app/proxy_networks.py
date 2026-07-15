"""Shared trusted-proxy network parsing for admin client source resolution."""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable

# Immediate peers (Render load balancer / private network) allowed to supply
# forwarding headers. Matches Render's in-cluster proxy addresses.
DEFAULT_TRUSTED_PROXY_IPS: tuple[str, ...] = (
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
)

# Cloudflare published IPv4/IPv6 ranges used to validate edge hops in XFF.
# https://www.cloudflare.com/ips/
DEFAULT_TRUSTED_FORWARDER_IPS: tuple[str, ...] = (
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

MAX_FORWARDING_CHAIN_LENGTH = 20

_FORWARDED_FOR_TOKEN = re.compile(
    r"^\s*(?:for=\s*)?(?P<value>[^;,\s]+|\"[^\"]+\")\s*",
    re.IGNORECASE,
)


def parse_network_list(values: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in values:
        item = raw.strip()
        if not item:
            continue
        if "/" not in item:
            address = normalize_ip_address(item)
            if address is None:
                continue
            prefix = 32 if address.version == 4 else 128
            networks.append(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Normalize IPv4/IPv6 strings deterministically; strip ports and brackets."""
    candidate = value.strip().strip('"').strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def format_normalized_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.compressed if address.version == 6 else str(address)


def address_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address in network for network in networks)


def split_forwarded_for(raw_header: str) -> list[str]:
    if len(raw_header) > 4096:
        return []
    tokens = [part.strip() for part in raw_header.split(",")]
    return [token for token in tokens if token]


def parse_forwarded_header(raw_header: str) -> list[str]:
    if len(raw_header) > 4096:
        return []
    values: list[str] = []
    for entry in raw_header.split(","):
        match = _FORWARDED_FOR_TOKEN.match(entry.strip())
        if match is None:
            continue
        raw_value = match.group("value").strip().strip('"')
        if raw_value.lower() == "unknown":
            continue
        normalized = normalize_ip_address(raw_value)
        if normalized is not None:
            values.append(format_normalized_ip(normalized))
    return values
