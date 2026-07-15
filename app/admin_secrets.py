"""Startup validation for admin authentication secrets."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_DISALLOWED_SECRET_VALUES = frozenset(
    {
        "changeme",
        "placeholder",
        "secret",
        "password",
        "admin",
        "test",
        "example",
        "dummy",
        "default",
    }
)


def _is_weak_secret(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _DISALLOWED_SECRET_VALUES:
        return True
    tokens = [token for token in re.split(r"[-_]+", lowered) if token]
    return bool(tokens) and all(token in _DISALLOWED_SECRET_VALUES for token in tokens)


def validate_admin_secret_value(
    value: str,
    *,
    env_name: str,
    required: bool = True,
) -> None:
    """Reject missing, weak, or placeholder admin secret material."""
    stripped = value.strip()
    if not stripped:
        if required:
            raise ValueError(f"{env_name} is required when admin authentication is configured")
        return
    if len(stripped) < MIN_ADMIN_SECRET_LENGTH:
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    if _is_weak_secret(stripped):
        raise ValueError(f"{env_name} uses a disallowed placeholder value")


def validate_admin_security_secrets(settings: Settings) -> None:
    """Fail fast when admin auth is enabled with weak limiter key material."""
    if not settings.admin_auth_configured:
        return
    validate_admin_secret_value(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        validate_admin_secret_value(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
            required=True,
        )
        if previous == settings.admin_login_limiter_secret.strip():
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET during rotation"
            )
