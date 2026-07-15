"""Fail-fast validation for admin authentication security settings."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES = 32

_PLACEHOLDER_SECRET_PATTERN = re.compile(
    r"(?i)(changeme|change-me|replace-me|placeholder|example|dummy|test-secret|your-secret|"
    r"admin_login_limiter_secret|insert-secret|todo|fixme|xxx+)"
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security settings are missing or weak."""


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, or placeholder limiter key material."""
    normalized = secret.strip()
    if not normalized:
        raise AdminSecurityConfigError(f"{env_name} is required")
    encoded = normalized.encode("utf-8")
    if len(encoded) < MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES:
        raise AdminSecurityConfigError(
            f"{env_name} must be at least {MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES} bytes"
        )
    if _PLACEHOLDER_SECRET_PATTERN.search(normalized):
        raise AdminSecurityConfigError(f"{env_name} must not use placeholder key material")


def validate_admin_login_limiter_settings(settings: Settings) -> None:
    """Validate limiter secrets when admin authentication is fully configured."""
    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        validate_admin_login_limiter_secret(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret:
            raise AdminSecurityConfigError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
            )
