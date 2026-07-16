"""Central read-only guard and startup validation for admin preview mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

if TYPE_CHECKING:
    from app.config import Settings

# Safe methods permitted for screenshot rendering under preview (RFC 9110).
PREVIEW_SAFE_METHODS = frozenset({"GET", "HEAD"})
PREVIEW_ALLOW_HEADER = "GET, HEAD"

# Production data-store and provider credentials that must stay empty in preview.
PREVIEW_FORBIDDEN_ENV_VARS: tuple[str, ...] = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "RESEND_API_KEY",
    "PLAUSIBLE_API_KEY",
)


class AdminPreviewConfigError(RuntimeError):
    """Raised when preview mode is enabled alongside production credentials."""


def validate_admin_preview_config(settings: Settings) -> None:
    """Fail fast when preview is active but production credentials are configured."""
    if not settings.admin_preview_enabled:
        return

    violations: list[str] = []
    for env_name in PREVIEW_FORBIDDEN_ENV_VARS:
        if (value := _env_value(env_name)).strip():
            violations.append(env_name)

    if settings.database_url.strip():
        if "DATABASE_URL" not in violations:
            violations.append("DATABASE_URL")

    if settings.stripe_secret_key.strip():
        if "STRIPE_SECRET_KEY" not in violations:
            violations.append("STRIPE_SECRET_KEY")
    if settings.stripe_webhook_secret.strip():
        if "STRIPE_WEBHOOK_SECRET" not in violations:
            violations.append("STRIPE_WEBHOOK_SECRET")
    if settings.resend_api_key.strip():
        if "RESEND_API_KEY" not in violations:
            violations.append("RESEND_API_KEY")
    if settings.plausible_api_key.strip():
        if "PLAUSIBLE_API_KEY" not in violations:
            violations.append("PLAUSIBLE_API_KEY")

    if violations:
        ordered = ", ".join(sorted(set(violations)))
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE cannot run with production credentials configured: "
            f"{ordered}"
        )


def _env_value(name: str) -> str:
    import os

    return os.environ.get(name, "")


def admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


class AdminPreviewReadOnlyMiddleware:
    """Deny unsafe /admin methods before routing when preview is enabled."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path = scope.get("path", "")
        method = str(scope.get("method", "GET")).upper()

        if settings.admin_preview_enabled and admin_path(path):
            if method not in PREVIEW_SAFE_METHODS:
                response = PlainTextResponse(
                    "Method Not Allowed",
                    status_code=405,
                    headers={"Allow": PREVIEW_ALLOW_HEADER},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
