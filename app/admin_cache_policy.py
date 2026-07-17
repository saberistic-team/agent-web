"""Central admin cache isolation policy (#337).

Every ``/admin`` response must carry ``Cache-Control: no-store, private`` so
CRM data, brief contents, audit records, session CSRF values, and login-flow
state are not stored or reused by browser or intermediary HTTP caches.

HTTP cache controls reduce storage and reuse but are **not** a secure erasure
guarantee — browser history, bfcache, screenshots, and OS memory may still
retain prior content outside the HTTP cache layer.
"""

from __future__ import annotations

from starlette.responses import Response

from app.admin_response_policy import apply_response_headers

# Authoritative directive: no-store prevents storage/reuse; private documents
# user-specific representations and blocks shared-cache storage if policy shifts.
ADMIN_CACHE_CONTROL = "no-store, private"


def admin_cache_headers() -> dict[str, str]:
    """Return the enforced admin Cache-Control header set."""
    return {"Cache-Control": ADMIN_CACHE_CONTROL}


def apply_admin_cache_headers(response: Response) -> None:
    """Attach admin cache isolation headers, replacing any prior Cache-Control."""
    apply_response_headers(response, admin_cache_headers())
