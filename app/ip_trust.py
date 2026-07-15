"""IP normalization and trusted-proxy network parsing."""

from __future__ import annotations

import ipaddress


def parse_trusted_proxy_networks(
    spec: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated proxy IPs and CIDR blocks."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
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


def normalize_client_address(raw: str) -> str | None:
    """Normalize IPv4/IPv6 addresses deterministically; reject malformed values."""
    text = raw.strip()
    if not text:
        return None

    if text.startswith("["):
        closing = text.find("]")
        if closing > 0:
            text = text[1:closing]
    elif text.count(":") == 1 and "." in text:
        host, _port = text.rsplit(":", 1)
        if host:
            text = host

    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return addr.compressed


def address_in_trusted_networks(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)
