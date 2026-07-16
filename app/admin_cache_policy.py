"""Central no-store cache policy for every /admin response (#337).

HTTP ``Cache-Control: no-store, private`` prevents browsers and shared caches
from storing or reusing admin HTML, JSON, login-flow state, and session-bound
CSRF values. This reduces accidental exposure via the HTTP cache and back/forward
navigation, but it is **not** a secure erasure guarantee — browser memory,
screenshots, and malicious intermediaries may still retain data.

Broader security headers (CSP, HSTS, etc.) are owned by issue #308.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

ADMIN_CACHE_CONTROL = "no-store, private"


def is_admin_path(path: str) -> bool:
    """Return True when the request path is under the admin surface."""
    return path == "/admin" or path.startswith("/admin/")


def apply_admin_cache_headers(response: Response) -> None:
    """Replace any existing Cache-Control with the admin no-store policy."""
    if "cache-control" in response.headers:
        del response.headers["cache-control"]
    response.headers["Cache-Control"] = ADMIN_CACHE_CONTROL


async def admin_no_store_cache_policy(request: Request, call_next):
    """Middleware: enforce no-store on every /admin response."""
    path = request.url.path
    response = await call_next(request)
    if is_admin_path(path):
        apply_admin_cache_headers(response)
    return response
