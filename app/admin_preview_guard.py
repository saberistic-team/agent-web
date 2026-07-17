"""Central read-only guard and startup validation for ADMIN_PREVIEW_MODE."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

# Safe methods permitted for screenshot rendering (RFC 9110).
PREVIEW_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
PREVIEW_ALLOW_HEADER = ", ".join(sorted(PREVIEW_ALLOWED_METHODS))

# Read-only-by-design POST endpoints that compute and return a response
# without persisting anything (verified against ADMIN_MUTATION_ROUTE_CLASSIFICATIONS'
# "intentionally_unaudited" entries) — exempt from the blanket unsafe-method
# block below so their own ADMIN_PREVIEW_MODE branch (returning fixed mock
# data) keeps working instead of being pre-empted by this guard.
PREVIEW_SAFE_UNSAFE_METHOD_PATHS: frozenset[str] = frozenset(
    {"/admin/imports/reconcile-preview"}
)

# Production data-store and provider credentials that must stay empty in preview.
# Plausible was retired repo-wide (#117/#273) — do not reintroduce a reference here.
PREVIEW_FORBIDDEN_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("DATABASE_URL", "database"),
    ("STRIPE_SECRET_KEY", "Stripe"),
    ("STRIPE_WEBHOOK_SECRET", "Stripe webhook"),
    ("RESEND_API_KEY", "email provider"),
)


class AdminPreviewConfigError(ValueError):
    """Raised when ADMIN_PREVIEW_MODE conflicts with production data stores."""


def validate_admin_preview_config(settings: Settings) -> None:
    """Reject preview mode when real database or provider credentials are configured."""
    if not settings.admin_preview_mode:
        return
    for env_name, label in PREVIEW_FORBIDDEN_ENV_VARS:
        value = (os.environ.get(env_name) or "").strip()
        if value:
            raise AdminPreviewConfigError(
                f"ADMIN_PREVIEW_MODE cannot run with {env_name} set ({label} access)"
            )


def _method_not_allowed_response_body() -> bytes:
    return b"Method Not Allowed"


async def _drain_request_body(receive) -> None:  # noqa: ANN001
    """Discard request body so the connection can be reused without handler parsing."""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        if not message.get("more_body", False):
            break


class AdminPreviewReadOnlyMiddleware:
    """Deny unsafe /admin methods while preview mode is active."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/admin"):
            await self.app(scope, receive, send)
            return

        from app.config import get_settings

        if not get_settings().admin_preview_enabled:
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        if method in PREVIEW_ALLOWED_METHODS or path in PREVIEW_SAFE_UNSAFE_METHOD_PATHS:
            await self.app(scope, receive, send)
            return

        await _drain_request_body(receive)
        headers = [
            (b"allow", PREVIEW_ALLOW_HEADER.encode("latin-1")),
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(_method_not_allowed_response_body())).encode()),
        ]
        await send({"type": "http.response.start", "status": 405, "headers": headers})
        await send(
            {
                "type": "http.response.body",
                "body": _method_not_allowed_response_body(),
                "more_body": False,
            }
        )
