"""Central admin cache isolation policy (#337).

Every ``/admin`` response must carry ``Cache-Control: no-store, private`` so
browser and intermediary HTTP caches do not store or reuse CRM data, brief
contents, audit records, session-derived CSRF values, or login-flow state.

HTTP cache directives reduce storage and reuse; they are not a secure erasure
guarantee for browser UI memory, screenshots, or malicious intermediaries.
"""

from __future__ import annotations

from starlette.responses import Response

from app.admin_response_policy import apply_response_headers

ADMIN_CACHE_CONTROL = "no-store, private"


def admin_cache_headers() -> dict[str, str]:
    """Return the reviewed admin cache-isolation header set."""
    return {"Cache-Control": ADMIN_CACHE_CONTROL}


def apply_admin_cache_headers(response: Response) -> None:
    """Attach the admin no-store cache policy, replacing weaker directives."""
    apply_response_headers(response, admin_cache_headers())
