"""Response header policies for sensitive admin HTML surfaces (#337, #308)."""

from __future__ import annotations

from starlette.responses import Response

ADMIN_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

ADMIN_BROWSER_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def apply_admin_sensitive_response_headers(response: Response) -> Response:
    """Apply no-store and browser security headers to an admin HTML response."""
    for key, value in {**ADMIN_NO_STORE_HEADERS, **ADMIN_BROWSER_SECURITY_HEADERS}.items():
        response.headers[key] = value
    return response
