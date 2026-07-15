"""Startup validation for admin security secrets."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

LIMITER_SECRET_MIN_LENGTH = 32

_PLACEHOLDER_LIMITER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "placeholder",
        "secret",
        "password",
        "admin",
        "test",
        "dev",
        "development",
        "example",
        "your-secret-here",
        "replace-me",
        "placeholder-placeholder-placeholder!",
    }
)


def validate_admin_login_limiter_secret(secret: str, *, env_name: str) -> None:
    """Fail fast when limiter key material is missing, weak, or a placeholder."""
    if not secret:
        raise ValueError(f"{env_name} is required when admin authentication is enabled")
    if len(secret) < LIMITER_SECRET_MIN_LENGTH:
        raise ValueError(f"{env_name} must be at least {LIMITER_SECRET_MIN_LENGTH} characters")
    if secret != secret.strip():
        raise ValueError(f"{env_name} must not contain leading or trailing whitespace")
    if any(ord(ch) < 32 for ch in secret):
        raise ValueError(f"{env_name} must not contain control characters")
    if len(set(secret)) == 1:
        raise ValueError(f"{env_name} must not be a low-entropy value")
    if secret.strip().lower() in _PLACEHOLDER_LIMITER_SECRETS:
        raise ValueError(f"{env_name} must not be a placeholder value")


def validate_admin_security_secrets(settings: Settings) -> None:
    """Validate admin security secrets before serving authenticated routes."""
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return

    validate_admin_login_limiter_secret(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_secret_previous
    if not previous:
        return

    validate_admin_login_limiter_secret(
        previous,
        env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if hmac.compare_digest(previous, settings.admin_login_limiter_secret):
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
            "ADMIN_LOGIN_LIMITER_SECRET"
        )
