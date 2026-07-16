"""Central read-only guard for /admin routes while ADMIN_PREVIEW_MODE is enabled."""

from __future__ import annotations

from app.config import get_settings

ALLOWED_METHODS = frozenset({"GET", "HEAD"})
ALLOW_HEADER = "GET, HEAD"


class AdminPreviewReadOnlyMiddleware:
    """Deny unsafe HTTP methods on /admin before handlers parse bodies or touch stores."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.admin_preview_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/admin"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in ALLOWED_METHODS:
            await self.app(scope, receive, send)
            return

        await _send_method_not_allowed(send)


async def _send_method_not_allowed(send) -> None:
    body = b"Method Not Allowed"
    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [
                (b"allow", ALLOW_HEADER.encode("ascii")),
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
