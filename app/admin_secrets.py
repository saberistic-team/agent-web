"""Fail-fast validation for admin security secrets."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

WEAK_ADMIN_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "password",
        "secret",
        "admin",
        "test",
        "placeholder",
        "replace-me",
        "your-secret-here",
        "admin-login-limiter-secret",
        "admin-session-secret",
    }
)


class AdminSecretValidationError(RuntimeError):
    """Raised when required admin secret material is missing or weak."""


def validate_admin_secret_value(value: str, *, name: str) -> None:
    """Reject missing, short, or known-weak admin secret values."""
    secret = value.strip()
    if not secret:
        raise AdminSecretValidationError(f"{name} is required when admin authentication is enabled")
    if len(secret) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecretValidationError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    if secret.lower() in WEAK_ADMIN_SECRETS:
        raise AdminSecretValidationError(f"{name} is a known weak or placeholder value")


def validate_admin_security_secrets(settings: Settings) -> None:
    """Validate admin secrets before serving authenticated routes."""
    if not settings.admin_auth_configured:
        return
    if settings.admin_preview_enabled and not settings.database_url:
        return

    validate_admin_secret_value(settings.admin_session_secret, name="ADMIN_SESSION_SECRET")

    if not settings.database_url:
        return

    validate_admin_secret_value(
        settings.admin_login_limiter_secret,
        name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_secret_previous.strip()
    if not previous:
        return
    validate_admin_secret_value(
        previous,
        name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if previous == settings.admin_login_limiter_secret:
        raise AdminSecretValidationError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
        )
