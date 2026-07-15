"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "changemechangemechangemechangeme",
        "change-me",
        "change_me",
        "placeholder",
        "replace-me",
        "replace_me",
        "secret",
        "admin",
        "password",
        "test",
        "testing",
        "dev",
        "development",
        "local",
        "example",
        "your-secret-here",
        "your_secret_here",
    }
)


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, placeholder, or malformed limiter key material."""
    normalized = secret.strip()
    if not normalized:
        raise ValueError(f"{env_name} is required when admin authentication uses Postgres")
    if len(normalized) < MIN_ADMIN_SECRET_LENGTH:
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters of random key material"
        )
    if normalized.lower() in _PLACEHOLDER_SECRETS:
        raise ValueError(f"{env_name} must not use a placeholder or dictionary value")


def validate_admin_security_at_startup(settings: Settings) -> None:
    """Validate admin security secrets before serving traffic."""
    if not settings.database_url:
        return
    if settings.admin_preview_enabled:
        return
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return

    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        validate_admin_login_limiter_secret(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret.strip():
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
            )
