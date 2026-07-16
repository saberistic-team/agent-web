"""Central read-only guard for admin screenshot preview mode (#331)."""

from __future__ import annotations

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

PREVIEW_SAFE_METHODS = frozenset({"GET", "HEAD"})
PREVIEW_ALLOWED_METHODS_HEADER = "GET, HEAD"
PREVIEW_DENIED_METHODS = frozenset(
    {"POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT", "OPTIONS"}
)


class AdminPreviewReadOnlyMiddleware:
    """Deny unsafe /admin methods while preview is enabled.

    Stateless ASGI middleware: rejects before body parsing, session mutation,
    database access, or provider calls. Route-level preview POST simulations are
    not the security boundary.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        settings = get_settings()
        if settings.admin_preview_enabled and path.startswith("/admin"):
            if method not in PREVIEW_SAFE_METHODS:
                response = Response(
                    content="Method Not Allowed",
                    status_code=405,
                    headers={"Allow": PREVIEW_ALLOWED_METHODS_HEADER},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
