"""Central admin cache isolation policy (#337).

Every ``/admin`` response must carry ``Cache-Control: no-store, private`` so
CRM data, brief contents, audit records, session CSRF values, and login-flow
state are not stored or reused by browser or intermediary HTTP caches.

HTTP cache controls reduce storage and reuse but are **not** a secure erasure
guarantee — they do not remove data from browser UI memory (bfcache), OS swap,
screenshots, or malicious intermediaries that ignore directives.
"""

from __future__ import annotations

from starlette.responses import Response

from app.admin_response_policy import apply_response_headers

ADMIN_CACHE_CONTROL = "no-store, private"


def admin_cache_headers() -> dict[str, str]:
    """Return the reviewed admin cache header set (single value per name)."""
    return {"Cache-Control": ADMIN_CACHE_CONTROL}


def apply_admin_cache_headers(response: Response) -> None:
    """Attach the admin no-store cache policy, replacing weaker downstream values."""
    apply_response_headers(response, admin_cache_headers())
