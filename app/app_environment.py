"""Explicit application environment selection (#307-compatible slice)."""

from __future__ import annotations

from enum import Enum


class AppEnvironment(str, Enum):
    """Deployment mode; preview bypass requires DEVELOPMENT or PREVIEW."""

    DEVELOPMENT = "development"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


PREVIEW_AUTH_ALLOWED_ENVIRONMENTS = frozenset(
    {AppEnvironment.DEVELOPMENT, AppEnvironment.PREVIEW}
)


def parse_app_environment(raw: str) -> AppEnvironment:
    """Parse ``APP_ENV``; default to development when unset (local/test default)."""
    normalized = (raw or "").strip().lower()
    if not normalized:
        return AppEnvironment.DEVELOPMENT
    try:
        return AppEnvironment(normalized)
    except ValueError as exc:
        raise ValueError(
            f"APP_ENV must be one of: {', '.join(e.value for e in AppEnvironment)}"
        ) from exc
