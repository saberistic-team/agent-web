"""Admin security configuration validation."""

from __future__ import annotations

import hmac
import re

from app.config import Settings

MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "changeme",
        "password",
        "placeholder",
        "secret",
        "test",
        "admin",
        "default",
        "example",
    }
)

_WEAK_SECRET_PATTERNS = (
    re.compile(r"changeme", re.IGNORECASE),
    re.compile(r"placeholder", re.IGNORECASE),
    re.compile(r"your[-_]?secret", re.IGNORECASE),
    re.compile(r"replace[-_]?me", re.IGNORECASE),
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security configuration is missing or weak."""


def _is_weak_secret(secret: str) -> bool:
    normalized = secret.strip()
    if len(normalized) < MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH:
        return True
    lowered = normalized.lower()
    if lowered in _WEAK_SECRET_LITERALS:
        return True
    if any(pattern.search(normalized) for pattern in _WEAK_SECRET_PATTERNS):
        return True
    if len(set(normalized)) < 4:
        return True
    return False


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    if not secret or not secret.strip():
        raise AdminSecurityConfigError(
            f"{env_name} is required for admin login rate limiting"
        )
    if _is_weak_secret(secret):
        raise AdminSecurityConfigError(
            f"{env_name} must be at least {MIN_ADMIN_LOGIN_LIMITER_SECRET_LENGTH} "
            "high-entropy bytes; weak or placeholder values are rejected"
        )


def validate_admin_security_config(settings: Settings) -> None:
    """Fail fast when admin auth is enabled outside preview mode."""
    if not settings.admin_auth_configured:
        return
    if settings.admin_preview_enabled:
        return
    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous.strip()
    if not previous:
        return
    validate_admin_login_limiter_secret(
        previous,
        env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if hmac.compare_digest(
        settings.admin_login_limiter_secret,
        settings.admin_login_limiter_secret_previous,
    ):
        raise AdminSecurityConfigError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
            "ADMIN_LOGIN_LIMITER_SECRET during rotation"
        )
