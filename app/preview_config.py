"""Cross-field validation for ADMIN_PREVIEW_MODE startup safety."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


class PreviewConfigError(ValueError):
    """Raised when preview mode is incompatible with production data-store config."""


# Production provider secrets that must not be present when preview mode is requested.
_PREVIEW_FORBIDDEN_SETTINGS: tuple[tuple[str, str], ...] = (
    ("database_url", "DATABASE_URL"),
    ("stripe_secret_key", "STRIPE_SECRET_KEY"),
    ("stripe_webhook_secret", "STRIPE_WEBHOOK_SECRET"),
    ("stripe_publishable_key", "STRIPE_PUBLISHABLE_KEY"),
    ("resend_api_key", "RESEND_API_KEY"),
    ("plausible_api_key", "PLAUSIBLE_API_KEY"),
)


def validate_preview_config(settings: Settings) -> None:
    """Fail fast when preview mode is combined with real database or provider credentials."""
    if not settings.admin_preview_mode:
        return

    violations: list[str] = []
    for attr, env_name in _PREVIEW_FORBIDDEN_SETTINGS:
        value = getattr(settings, attr, "")
        if isinstance(value, str) and value.strip():
            violations.append(env_name)

    if violations:
        joined = ", ".join(sorted(violations))
        raise PreviewConfigError(
            f"ADMIN_PREVIEW_MODE cannot run with production credentials configured: {joined}"
        )
