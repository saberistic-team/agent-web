"""Admin security configuration validation (limiter secrets, fail-fast startup)."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH = 32

_WEAK_SECRET_MARKERS = frozenset(
    {
        "changeme",
        "change-me",
        "example",
        "password",
        "placeholder",
        "replace",
        "secret-here",
        "test-secret",
        "your-secret",
    }
)

_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(changeme|change[_-]?me|example|placeholder|replace[_-]?me|"
    r"secret[_-]?here|your[_-]?secret|xxx+|todo)"
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security secrets are missing or too weak."""


def _validate_limiter_secret_value(
    secret: str,
    *,
    env_name: str,
    required: bool,
) -> None:
    trimmed = secret.strip()
    if not trimmed:
        if required:
            raise AdminSecurityConfigError(f"{env_name} is required")
        return
    if len(trimmed) < MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH:
        raise AdminSecurityConfigError(
            f"{env_name} must be at least {MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH} characters"
        )
    lowered = trimmed.lower()
    if lowered in _WEAK_SECRET_MARKERS:
        raise AdminSecurityConfigError(f"{env_name} must not use a documented placeholder value")
    if _PLACEHOLDER_PATTERN.search(trimmed):
        raise AdminSecurityConfigError(f"{env_name} must not use placeholder key material")


def should_validate_admin_security(settings: Settings) -> bool:
    """Return whether startup should fail fast on weak admin security secrets."""
    if not (settings.admin_username and settings.admin_password_hash):
        return False
    if settings.admin_preview_enabled:
        return False
    return True


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin login limiter secrets before serving authenticated routes."""
    if not should_validate_admin_security(settings):
        return

    _validate_limiter_secret_value(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
        required=True,
    )
    _validate_limiter_secret_value(
        settings.admin_login_limiter_previous_secret,
        env_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
        required=False,
    )
