"""Default trusted-proxy CIDRs for production (Cloudflare -> Render -> Uvicorn)."""

from __future__ import annotations

# Render internal / loopback peers that terminate TLS before Uvicorn.
RENDER_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
    "::1/128",
)

# Cloudflare published IPv4 ranges (https://www.cloudflare.com/ips-v4).
# Used to skip Cloudflare hops when walking X-Forwarded-For right-to-left and to
# verify CF-Connecting-IP came through the documented edge.
CLOUDFLARE_PROXY_CIDRS: tuple[str, ...] = (
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
)

# Uvicorn --forwarded-allow-ips value matching the Render hop boundary.
UVICORN_FORWARDED_ALLOW_IPS: str = ",".join(RENDER_TRUSTED_PROXY_CIDRS)

# Production ADMIN_TRUSTED_PROXY_CIDRS: Render peers plus Cloudflare edge hops.
PRODUCTION_TRUSTED_PROXY_CIDRS: str = ",".join(
    (*RENDER_TRUSTED_PROXY_CIDRS, *CLOUDFLARE_PROXY_CIDRS)
)

# Subset used only to prove CF-Connecting-IP came through Cloudflare.
PRODUCTION_CLOUDFLARE_PROXY_CIDRS: str = ",".join(CLOUDFLARE_PROXY_CIDRS)


def parse_trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated trusted-proxy CIDR list."""
    if not raw.strip():
        return ()
    return tuple(
        part.strip()
        for part in raw.split(",")
        if part.strip()
    )
