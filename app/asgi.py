"""Production ASGI entrypoint with explicit trusted-proxy boundaries."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.client_source import RENDER_TRUSTED_PROXY_CIDRS
from app.config import get_settings
from app.main import app as fastapi_app

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class _PeerCaptureMiddleware:
    """Preserve the raw TCP peer before proxy-header rewriting."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            if client is not None:
                state = scope.setdefault("state", {})
                state["asgi_peer_host"] = client[0]
        await self.app(scope, receive, send)


def _forwarded_allow_hosts() -> list[str]:
    settings = get_settings()
    configured = settings.admin_forwarded_allow_ips.strip()
    if configured:
        return [entry.strip() for entry in configured.split(",") if entry.strip()]
    if settings.admin_trust_proxy_headers and settings.admin_trusted_proxy_ips.strip():
        return [
            entry.strip()
            for entry in settings.admin_trusted_proxy_ips.split(",")
            if entry.strip()
        ]
    return list(RENDER_TRUSTED_PROXY_CIDRS)


_trusted_hosts = _forwarded_allow_hosts()
app: Callable[[Scope, Receive, Send], Awaitable[None]] = _PeerCaptureMiddleware(
    ProxyHeadersMiddleware(fastapi_app, trusted_hosts=_trusted_hosts)
)
