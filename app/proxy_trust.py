"""Shared trusted-proxy constants and network parsing."""

from __future__ import annotations

import ipaddress

# Production Render private-network proxy boundary (overridable via env).
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

# Uvicorn forwarded-header trust for Render deployments (version-controlled).
DEFAULT_UVICORN_FORWARDED_ALLOW_IPS = ",".join(DEFAULT_TRUSTED_PROXY_CIDRS)


def parse_trusted_proxy_networks(
    raw: str | None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated proxy CIDRs/addresses into network objects."""
    if not raw or not raw.strip():
        return tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_TRUSTED_PROXY_CIDRS
        )
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                addr = ipaddress.ip_address(token)
                prefix = 32 if addr.version == 4 else 128
                networks.append(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)
