"""Admin HTML response policies (#337 no-store, #308 browser security headers)."""

from __future__ import annotations

from fastapi.responses import HTMLResponse, Response

ADMIN_NO_STORE_CACHE_CONTROL = "no-store"

ADMIN_BROWSER_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def apply_admin_response_policy(response: Response) -> Response:
    """Apply shared admin cache and browser security headers to a response."""
    response.headers["Cache-Control"] = ADMIN_NO_STORE_CACHE_CONTROL
    for header, value in ADMIN_BROWSER_SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


def admin_html_response(content: str, *, status_code: int = 200) -> HTMLResponse:
    """Return an admin HTML response with #337/#308 policies applied."""
    return apply_admin_response_policy(HTMLResponse(content, status_code=status_code))
