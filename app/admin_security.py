"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

import hmac

from app.config import Settings

ADMIN_LOGIN_LIMITER_SECRET_MIN_LENGTH = 32

_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "replace-me",
        "replace_me",
        "placeholder",
        "secret",
        "test",
        "testing",
        "password",
        "admin",
    }
)


def validate_admin_login_limiter_secret(
    value: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> str:
    """Return normalized limiter secret or raise ``ValueError`` when invalid."""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{env_name} is required when admin authentication is enabled")
    if normalized.lower() in _PLACEHOLDER_SECRETS:
        raise ValueError(f"{env_name} must not be a placeholder value")
    if len(normalized) < ADMIN_LOGIN_LIMITER_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{env_name} must be at least {ADMIN_LOGIN_LIMITER_SECRET_MIN_LENGTH} characters"
        )
    return normalized


def validate_admin_security_secrets(settings: Settings) -> None:
    """Validate admin security material before serving authenticated routes."""
    if not settings.admin_auth_configured:
        return

    current = validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous.strip()
    if not previous:
        return

    previous_normalized = validate_admin_login_limiter_secret(
        previous,
        env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if hmac.compare_digest(previous_normalized, current):
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
        )
