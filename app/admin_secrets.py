"""Fail-fast validation for admin security secrets."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES = 32

_PLACEHOLDER_LIMITER_SECRETS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "placeholder",
        "secret",
        "test",
        "admin",
        "password",
        "admin-login-limiter-secret",
        "admin_login_limiter_secret",
    }
)

_WEAK_SECRET_PATTERN = re.compile(r"^(.)\1{7,}$")


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    env_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, or placeholder limiter key material."""
    normalized = secret.strip()
    if not normalized:
        raise ValueError(f"{env_name} is required when admin authentication is configured")
    if len(normalized.encode("utf-8")) < MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES:
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES} bytes"
        )
    lowered = normalized.lower()
    if lowered in _PLACEHOLDER_LIMITER_SECRETS:
        raise ValueError(f"{env_name} must not use placeholder key material")
    if _WEAK_SECRET_PATTERN.match(normalized):
        raise ValueError(f"{env_name} must not use repeated-character key material")


def validate_admin_security_secrets(settings: Settings) -> None:
    """Validate admin security secrets when operator authentication is configured."""
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return
    if settings.admin_preview_mode and not settings.database_url:
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
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET during rotation"
            )
