"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

import hmac

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

KNOWN_WEAK_SECRETS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "replace-me",
        "your-secret-here",
        "secret",
        "test",
        "password",
    }
)


class AdminSecurityConfigError(ValueError):
    """Admin security configuration is missing or too weak."""


def validate_admin_secret(env_name: str, value: str) -> None:
    """Reject missing, short, or placeholder admin secret material."""
    if not value or not value.strip():
        raise AdminSecurityConfigError(f"{env_name} is required")
    if len(value) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecurityConfigError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    normalized = value.strip().lower()
    if normalized in KNOWN_WEAK_SECRETS:
        raise AdminSecurityConfigError(f"{env_name} must not use a placeholder value")
    if "placeholder" in normalized:
        raise AdminSecurityConfigError(f"{env_name} must not use a placeholder value")


def validate_admin_security_settings(settings: Settings) -> None:
    """Validate admin secrets before serving authenticated routes."""
    validate_admin_secret("ADMIN_SESSION_SECRET", settings.admin_session_secret)
    validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET", settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_previous_secret
    if previous:
        validate_admin_secret("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", previous)
        if hmac.compare_digest(settings.admin_login_limiter_secret, previous):
            raise AdminSecurityConfigError(
                "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
