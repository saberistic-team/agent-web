"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

import hmac

from app.config import Settings

MIN_ADMIN_LIMITER_SECRET_LENGTH = 32

_WEAK_SECRET_EXACT = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "replace-me",
        "placeholder",
        "example",
        "secret",
        "password",
        "admin",
        "test",
        "testing",
        "development",
        "dev",
    }
)

_WEAK_SECRET_SUBSTRINGS = (
    "changeme",
    "replace-me",
    "your-secret",
    "insert-secret",
    "placeholder",
)


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    field_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, or placeholder limiter key material."""
    normalized = (secret or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) < MIN_ADMIN_LIMITER_SECRET_LENGTH:
        raise ValueError(
            f"{field_name} must be at least {MIN_ADMIN_LIMITER_SECRET_LENGTH} characters"
        )
    lowered = normalized.lower()
    if lowered in _WEAK_SECRET_EXACT:
        raise ValueError(f"{field_name} must not use a placeholder value")
    for fragment in _WEAK_SECRET_SUBSTRINGS:
        if fragment in lowered:
            raise ValueError(f"{field_name} must not use a placeholder value")
    if len(set(normalized)) < 8:
        raise ValueError(f"{field_name} must contain sufficient entropy")


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin security secrets before serving authenticated routes."""
    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = (settings.admin_login_limiter_previous_secret or "").strip()
    if not previous:
        return
    validate_admin_login_limiter_secret(
        previous,
        field_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
    )
    current = settings.admin_login_limiter_secret.strip()
    if hmac.compare_digest(previous, current):
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
            "ADMIN_LOGIN_LIMITER_SECRET"
        )
