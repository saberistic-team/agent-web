"""Startup validation for admin authentication secrets."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "changeme",
        "change-me",
        "placeholder",
        "replace-me",
        "secret",
        "admin",
        "password",
        "test",
        "dev",
        "local",
    }
)


class AdminSecurityConfigurationError(ValueError):
    """Raised when required admin security configuration is missing or weak."""


def _validate_admin_secret_value(
    value: str,
    *,
    env_name: str,
    required: bool,
) -> None:
    normalized = value.strip()
    if not normalized:
        if required:
            raise AdminSecurityConfigurationError(f"{env_name} is required when admin auth is configured")
        return
    if len(normalized) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecurityConfigurationError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    if normalized.lower() in _WEAK_SECRET_LITERALS:
        raise AdminSecurityConfigurationError(f"{env_name} must not use a placeholder value")


def validate_admin_security_settings(settings: Settings) -> None:
    """Fail fast when admin auth is configured with weak limiter key material."""
    if not settings.admin_username:
        return
    _validate_admin_secret_value(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
        required=True,
    )
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        _validate_admin_secret_value(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
            required=True,
        )
        if previous == settings.admin_login_limiter_secret.strip():
            raise AdminSecurityConfigurationError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
            )
