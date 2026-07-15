"""Startup validation for admin security secrets."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_PLACEHOLDER_PATTERN = re.compile(
    r"^(changeme|replace.?me|your.?secret|secret|test|admin|password|placeholder|xxx+|000+)$",
    re.IGNORECASE,
)


def _is_weak_secret(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < MIN_ADMIN_SECRET_LENGTH:
        return True
    if _PLACEHOLDER_PATTERN.match(stripped):
        return True
    if len(set(stripped)) == 1:
        return True
    return False


def validate_admin_login_limiter_secret(
    value: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    if not value or not value.strip():
        raise ValueError(f"{env_name} is required when admin authentication is configured")
    if _is_weak_secret(value):
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters "
            "and must not be a placeholder or low-entropy value"
        )


def validate_admin_security_secrets(settings: Settings) -> None:
    """Fail fast when admin credentials are configured with weak limiter secrets."""
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return

    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous.strip()
    if not previous:
        return

    validate_admin_login_limiter_secret(
        previous,
        env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
    )
    if previous == settings.admin_login_limiter_secret.strip():
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
        )
