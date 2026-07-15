"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

from app.config import Settings

ADMIN_SECRET_MIN_LENGTH = 32

_WEAK_SECRET_MARKERS = frozenset(
    {
        "changeme",
        "change-me",
        "placeholder",
        "replace-me",
        "example",
        "your-secret",
        "set-me",
        "todo",
    }
)


def validate_admin_login_limiter_secret(value: str, *, env_name: str) -> None:
    """Reject missing, weak, or placeholder limiter key material."""
    secret = value.strip()
    if not secret:
        raise ValueError(f"{env_name} is required when admin authentication is configured")
    if len(secret) < ADMIN_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{env_name} must be at least {ADMIN_SECRET_MIN_LENGTH} characters"
        )
    lowered = secret.lower()
    for marker in _WEAK_SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{env_name} must not contain placeholder values")


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin security secrets at process startup."""
    if not settings.admin_auth_configured:
        return
    validate_admin_login_limiter_secret(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_previous_secret.strip()
    if previous:
        validate_admin_login_limiter_secret(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
        )
        if previous == settings.admin_login_limiter_secret.strip():
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
