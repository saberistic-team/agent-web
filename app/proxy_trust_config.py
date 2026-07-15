"""Shared proxy-trust configuration helpers."""

from __future__ import annotations

# Render / private-network defaults when proxy trust is enabled without explicit CIDRs.
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1",
    "::1",
)


def parse_cidr_list(raw: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse comma-separated CIDRs/IPs; ignore empty tokens."""
    if not raw.strip():
        return default
    parts = [part.strip() for part in raw.split(",")]
    return tuple(part for part in parts if part)


def production_trusted_proxy_cidrs() -> str:
    """Canonical trusted-proxy CIDR string for deployment configuration."""
    return ",".join(DEFAULT_TRUSTED_PROXY_CIDRS)
