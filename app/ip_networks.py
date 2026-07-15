"""IP network parsing helpers shared by settings and client source resolution."""

from __future__ import annotations

import ipaddress


def parse_networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDRs and host IPs into network objects."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
            else:
                address = ipaddress.ip_address(entry)
                prefix = 32 if address.version == 4 else 128
                networks.append(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)
