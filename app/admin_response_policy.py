"""Central admin response security-header, CSP, and cache policy (#308, #337).

Security headers (CSP, nosniff, frame denial, etc.) satisfy #308. Admin cache
isolation (``Cache-Control: no-store, private``) satisfies #337 — both are
applied from the same middleware entry point in ``app/main.py``.
"""

from __future__ import annotations

import base64
import secrets
from typing import Mapping

from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings

# 16 bytes = 128 bits (W3C CSP minimum nonce entropy).
_CSP_NONCE_BYTES = 16

# One-year HSTS max-age; includeSubDomains omitted until all subdomains are HTTPS.
_HSTS_MAX_AGE_SECONDS = 31_536_000

# Reviewed admin CSP directive inventory (see issue #308 asset audit).
_CSP_DIRECTIVES: dict[str, str] = {
    "default-src": "'none'",
    "base-uri": "'none'",
    "object-src": "'none'",
    "frame-ancestors": "'none'",
    "form-action": "'self'",
    "script-src": "'self'",
    "style-src": "'self' https://fonts.googleapis.com",
    "font-src": "'self' https://fonts.gstatic.com",
    "img-src": "'self'",
    "connect-src": "'self'",
}

# Enforced minimum directive names that must appear in every admin CSP.
_REQUIRED_CSP_DIRECTIVE_NAMES: frozenset[str] = frozenset(_CSP_DIRECTIVES)

# Disallowed CSP tokens unless explicitly reviewed (unit-tested).
_FORBIDDEN_CSP_TOKENS: frozenset[str] = frozenset(
    {"*", "'unsafe-eval'", "unsafe-eval", "'unsafe-inline'", "unsafe-inline"}
)

_PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), bluetooth=(), camera=(), "
    "display-capture=(), encrypted-media=(), fullscreen=(), geolocation=(), "
    "gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), "
    "picture-in-picture=(), publickey-credentials-get=(), screen-wake-lock=(), "
    "sync-xhr=(), usb=(), web-share=(), xr-spatial-tracking=()"
)

# Bounded enforcement plan: report-only is not used; full policy is enforced
# after automated browser verification (see docs/ADMIN_SECURITY_HEADERS.md).
CSP_ENFORCEMENT_OWNER = "agent-web maintainers"
CSP_ENFORCEMENT_DEADLINE = "2026-08-01"

# Authoritative admin cache directive (#337). ``no-store`` prevents storage and
# reuse; ``private`` documents user-specific content for shared caches.
ADMIN_CACHE_CONTROL = "no-store, private"


def is_admin_path(path: str) -> bool:
    """Return True when the request path is part of the admin surface."""
    return path == "/admin" or path.startswith("/admin/")


def generate_csp_nonce() -> str:
    """Return a URL-safe nonce with at least 128 bits of entropy."""
    return secrets.token_urlsafe(_CSP_NONCE_BYTES)


def csp_nonce_from_request(request: Request) -> str:
    """Return the per-response CSP nonce set by middleware, if any."""
    return getattr(request.state, "csp_nonce", "")


def nonce_entropy_bits(nonce: str) -> int:
    """Return decoded nonce entropy in bits (for tests)."""
    padding = "=" * (-len(nonce) % 4)
    raw = base64.urlsafe_b64decode(nonce + padding)
    return len(raw) * 8


def build_admin_csp(*, nonce: str) -> str:
    """Compose the enforced admin Content-Security-Policy value."""
    if not nonce:
        raise ValueError("admin CSP requires a per-response nonce")
    directives = dict(_CSP_DIRECTIVES)
    directives["script-src"] = f"'self' 'nonce-{nonce}'"
    policy = "; ".join(f"{name} {value}" for name, value in directives.items())
    validate_admin_csp(policy)
    return policy


def parse_csp_directives(policy: str) -> dict[str, str]:
    """Parse a semicolon-delimited CSP string into directive → value."""
    result: dict[str, str] = {}
    for part in policy.split(";"):
        piece = part.strip()
        if not piece:
            continue
        name, _, value = piece.partition(" ")
        result[name.strip().lower()] = value.strip()
    return result


def validate_admin_csp(policy: str) -> None:
    """Reject unreviewed wildcard/unsafe CSP and require explicit directives."""
    lowered = policy.lower()
    for token in _FORBIDDEN_CSP_TOKENS:
        if token in lowered:
            raise ValueError(f"disallowed CSP token: {token}")
    parsed = parse_csp_directives(policy)
    missing = _REQUIRED_CSP_DIRECTIVE_NAMES - parsed.keys()
    if missing:
        raise ValueError(f"missing required CSP directives: {sorted(missing)}")
    if parsed.get("frame-ancestors") != "'none'":
        raise ValueError("frame-ancestors must be 'none' for admin")
    script_src = parsed.get("script-src", "")
    if "'nonce-" not in script_src:
        raise ValueError("script-src must include a nonce source")


def admin_security_headers(settings: Settings, *, nonce: str) -> dict[str, str]:
    """Return the reviewed admin security header set (single value per name)."""
    headers = {
        "Content-Security-Policy": build_admin_csp(nonce=nonce),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": _PERMISSIONS_POLICY,
        "X-Frame-Options": "DENY",
        # Disable legacy XSS auditor; CSP is the modern control.
        "X-XSS-Protection": "0",
    }
    if hsts_enabled(settings):
        headers["Strict-Transport-Security"] = (
            f"max-age={_HSTS_MAX_AGE_SECONDS}"
        )
    return headers


def hsts_enabled(settings: Settings) -> bool:
    """HSTS only when BASE_URL is validated HTTPS (production/preview HTTPS)."""
    return settings.base_url.startswith("https://")


def static_asset_security_headers() -> dict[str, str]:
    """Minimal headers for fingerprinted static assets (no admin document CSP)."""
    return {"X-Content-Type-Options": "nosniff"}


def apply_response_headers(
    response: Response,
    headers: Mapping[str, str],
) -> None:
    """Set headers once, replacing any prior value for the same name."""
    for name, value in headers.items():
        if name in response.headers:
            del response.headers[name]
        response.headers[name] = value


def admin_cache_headers() -> dict[str, str]:
    """Return the enforced admin cache-isolation header set."""
    return {"Cache-Control": ADMIN_CACHE_CONTROL}


def apply_admin_security_headers(
    response: Response,
    settings: Settings,
    *,
    nonce: str,
) -> None:
    """Attach the full admin security header policy to a response."""
    apply_response_headers(response, admin_security_headers(settings, nonce=nonce))


def apply_admin_cache_headers(response: Response) -> None:
    """Attach no-store cache isolation, replacing any weaker downstream value."""
    apply_response_headers(response, admin_cache_headers())


def apply_static_asset_headers(response: Response) -> None:
    """Attach MIME-sniff protection to static asset responses."""
    apply_response_headers(response, static_asset_security_headers())
