"""Central admin cache isolation policy (#337).

``Cache-Control: no-store, private`` prevents browsers and shared caches from
storing or reusing admin and authentication responses. This reduces the risk
that CRM data, brief contents, audit records, session CSRF values, or login-flow
state are replayed from HTTP caches after logout or session expiry.

HTTP cache directives are not a secure erasure guarantee: back-forward cache,
in-memory tab state, screenshots, and compromised intermediaries may still
retain sensitive content outside the scope of this policy.
"""

from __future__ import annotations

from starlette.responses import Response

from app.admin_response_policy import apply_response_headers

ADMIN_CACHE_CONTROL = "no-store, private"


def admin_cache_headers() -> dict[str, str]:
    """Return the authoritative admin Cache-Control policy."""
    return {"Cache-Control": ADMIN_CACHE_CONTROL}


def apply_admin_cache_headers(response: Response) -> None:
    """Attach no-store cache isolation to an admin response."""
    apply_response_headers(response, admin_cache_headers())
