"""Shared trusted-proxy constants and network parsing."""

from __future__ import annotations

import ipaddress

DEFAULT_TRUSTED_PROXY_IPS = (
    "10.0.0.0/8,"
    "172.16.0.0/12,"
    "192.168.0.0/16,"
    "100.64.0.0/10,"
    "::1/128"
)


def parse_trusted_proxy_networks(
    spec: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated IP addresses and CIDR blocks."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in spec.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                addr = ipaddress.ip_address(entry)
                networks.append(
                    ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
                )
        except ValueError:
            continue
    return tuple(networks)
