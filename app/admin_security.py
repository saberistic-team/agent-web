"""Fail-fast validation for admin security secrets."""

from __future__ import annotations

import hmac
import re

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "changeme",
        "change-me",
        "replace-me",
        "placeholder",
        "your-secret-here",
        "secret",
        "password",
        "admin",
        "example",
    }
)

_PLACEHOLDER_PATTERN = re.compile(
    r"(changeme|replace[_-]?me|your[_-]?secret|placeholder|example[_-]?secret)",
    re.IGNORECASE,
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security configuration is invalid."""


def _validate_secret_strength(name: str, value: str) -> None:
    if not value:
        raise AdminSecurityConfigError(f"{name} is required when admin authentication is configured")
    if len(value) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecurityConfigError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    normalized = value.strip().lower()
    if normalized in _WEAK_SECRET_LITERALS:
        raise AdminSecurityConfigError(f"{name} appears to be a weak placeholder value")
    if _PLACEHOLDER_PATTERN.search(value):
        raise AdminSecurityConfigError(f"{name} appears to be a placeholder value")


def validate_admin_login_limiter_secret(value: str, *, name: str = "ADMIN_LOGIN_LIMITER_SECRET") -> None:
    """Validate limiter HMAC key material."""
    _validate_secret_strength(name, value)


def validate_admin_security_secrets(settings: Settings) -> None:
    """Validate admin secrets at startup when production admin auth is active."""
    if not settings.database_url or settings.admin_preview_enabled:
        return
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return

    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous
    if not previous:
        return
    validate_admin_login_limiter_secret(
        previous,
        name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if hmac.compare_digest(previous, settings.admin_login_limiter_secret):
        raise AdminSecurityConfigError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
        )
