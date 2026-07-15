"""Shared trusted-proxy constants for deployment and admin login source resolution."""

from __future__ import annotations

# RFC1918, loopback, and link-local ranges used by Render's internal proxy.
DEFAULT_RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/).
DEFAULT_CLOUDFLARE_PROXY_CIDRS: tuple[str, ...] = (
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


def parse_cidr_list(raw: str) -> tuple[str, ...]:
    """Split comma-separated CIDRs, ignoring empty tokens."""
    return tuple(token.strip() for token in raw.split(",") if token.strip())


def production_trusted_proxy_cidrs() -> tuple[str, ...]:
    """Default production proxy boundary: Render internal + Cloudflare edge."""
    return DEFAULT_RENDER_TRUSTED_PROXY_CIDRS + DEFAULT_CLOUDFLARE_PROXY_CIDRS


def default_uvicorn_forwarded_allow_ips() -> str:
    """Uvicorn peers allowed to supply forwarded headers (Render internal only)."""
    return ",".join(DEFAULT_RENDER_TRUSTED_PROXY_CIDRS)
