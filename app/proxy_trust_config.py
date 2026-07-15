"""Shared trusted-proxy configuration defaults and parsing helpers."""

from __future__ import annotations

# RFC1918 + loopback: Render load balancer and local dev peers.
DEFAULT_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

# Cloudflare published egress ranges (https://www.cloudflare.com/ips-v4 / ips-v6).
DEFAULT_TRUSTED_EDGE_CIDRS: tuple[str, ...] = (
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

DEFAULT_UVICORN_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"


def parse_cidr_list(raw: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated CIDR list; fall back to ``default`` when empty."""
    if raw is None or not raw.strip():
        return default
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(parts) if parts else default
