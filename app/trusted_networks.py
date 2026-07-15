"""Parse trusted proxy and edge CIDR specifications."""

from __future__ import annotations

import ipaddress


def parse_trusted_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated IPs or CIDR blocks; ignore malformed entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in (part.strip() for part in spec.split(",") if part.strip()):
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
