"""Startup validation for admin security configuration."""

from __future__ import annotations

from app.admin_secrets import validate_admin_login_limiter_secret
from app.config import Settings


def validate_admin_security_config(settings: Settings) -> None:
    """Fail fast when admin auth is enabled with weak or missing limiter secrets.

    Called during application startup when ``DATABASE_URL`` is configured and
    operator credentials are present. Does not log secret values.
    """
    if not settings.database_url:
        return
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return
    validate_admin_login_limiter_secret(
        settings.admin_login_limiter_secret,
        previous=settings.admin_login_limiter_secret_previous or None,
    )
