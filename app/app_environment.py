"""Explicit application environment selection (compatible with #307)."""

from __future__ import annotations

from enum import Enum


class AppEnvironment(str, Enum):
    DEVELOPMENT = "development"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


PREVIEW_ALLOWED_ENVIRONMENTS = frozenset(
    {AppEnvironment.DEVELOPMENT, AppEnvironment.PREVIEW}
)


def parse_app_environment(raw: str) -> AppEnvironment:
    """Parse ``APP_ENV`` / ``ANALYTICS_ENV`` into a typed environment."""
    normalized = (raw or "").strip().lower() or AppEnvironment.DEVELOPMENT.value
    try:
        return AppEnvironment(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported application environment: {normalized!r}") from exc
