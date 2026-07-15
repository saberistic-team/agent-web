"""Trusted proxy network parsing for admin client source resolution."""

from __future__ import annotations

import ipaddress


def parse_trusted_proxy_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated trusted proxy CIDRs and host IPs."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                addr = ipaddress.ip_address(token)
                prefix = 32 if isinstance(addr, ipaddress.IPv4Address) else 128
                networks.append(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)
