"""IP normalization and trusted-network parsing helpers."""

from __future__ import annotations

import ipaddress
from typing import Iterable


def parse_trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs and host addresses into networks."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                networks.append(ipaddress.ip_network(part, strict=False))
            else:
                addr = ipaddress.ip_address(part)
                prefix = 32 if addr.version == 4 else 128
                networks.append(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def normalize_ip(value: str) -> str | None:
    """Normalize IPv4/IPv6 addresses, ports, and IPv4-mapped IPv6 forms."""
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.startswith("[") and "]" in candidate:
        host, _, remainder = candidate[1:].partition("]")
        if remainder.startswith(":"):
            candidate = host
        else:
            candidate = host

    if candidate.count(":") == 1 and "." in candidate:
        host, _, port = candidate.partition(":")
        if port.isdigit():
            candidate = host

    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return str(parsed)


def ip_in_trusted_networks(
    value: str,
    trusted_networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = normalize_ip(value)
    if normalized is None:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks)
