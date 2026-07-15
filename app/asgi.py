"""ASGI entrypoint with verified proxy trust for admin client-source resolution."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import get_settings
from app.main import app as fastapi_app
from app.proxy_trust import DEFAULT_TRUSTED_PROXY_IPS

ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]],
    Awaitable[None],
]


class ImmediatePeerMiddleware:
    """Capture the raw TCP peer before proxy-header rewriting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope.get("type") in {"http", "websocket"}:
            client = scope.get("client")
            if client is not None:
                scope["immediate_peer"] = client[0]
        await self.app(scope, receive, send)


def build_asgi_app() -> ASGIApp:
    """Return the production ASGI stack (ImmediatePeer → ProxyHeaders → FastAPI).

    Uvicorn must be started with ``--no-proxy-headers`` so this stack captures
    the raw TCP peer before applying the verified forwarding-header boundary.
    """
    settings = get_settings()
    trusted_hosts = settings.admin_trusted_proxy_ips or DEFAULT_TRUSTED_PROXY_IPS
    stack: ASGIApp = fastapi_app
    if settings.admin_trust_proxy_headers:
        stack = ProxyHeadersMiddleware(stack, trusted_hosts=trusted_hosts)
    return ImmediatePeerMiddleware(stack)


app = build_asgi_app()
