"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

import hmac

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "placeholder",
        "secret",
        "password",
        "admin",
        "test",
        "development",
        "dev",
        "example",
        "sample",
        "dummy",
        "default",
    }
)


def _is_weak_secret(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < MIN_ADMIN_SECRET_LENGTH:
        return True
    lower = stripped.lower()
    if lower in _WEAK_SECRET_LITERALS:
        return True
    if lower.startswith("changeme") or lower.startswith("change-me"):
        return True
    if len(set(stripped)) == 1:
        return True
    return False


def validate_admin_secret(name: str, value: str) -> None:
    """Reject missing, short, or placeholder admin secret material."""
    if _is_weak_secret(value):
        raise ValueError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters "
            "and must not be a placeholder or low-entropy value"
        )


def should_validate_admin_security(settings: Settings) -> bool:
    """True when a production admin deployment is configured."""
    return bool(
        settings.database_url
        and settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    )


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin secrets before serving authenticated routes."""
    if not should_validate_admin_security(settings):
        return

    validate_admin_secret("ADMIN_SESSION_SECRET", settings.admin_session_secret)
    validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET", settings.admin_login_limiter_secret)

    previous = settings.admin_login_limiter_secret_previous
    if not previous:
        return

    validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous)
    if hmac.compare_digest(previous, settings.admin_login_limiter_secret):
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
        )
