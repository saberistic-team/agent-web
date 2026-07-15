"""Fail-fast validation for admin authentication secrets."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_BYTES = 32

_FORBIDDEN_SECRET_VALUES = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "password",
        "secret",
        "test",
        "testing",
        "placeholder",
        "placeholder-placeholder-placehold!",
        "admin",
        "admin-login-limiter-secret",
        "admin_session_secret",
    }
)


def _validate_secret_material(value: str, *, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} is required when admin authentication is configured")
    encoded = value.encode("utf-8")
    if len(encoded) < MIN_ADMIN_SECRET_BYTES:
        raise ValueError(f"{name} must be at least {MIN_ADMIN_SECRET_BYTES} bytes")
    normalized = value.strip().lower()
    if normalized in _FORBIDDEN_SECRET_VALUES:
        raise ValueError(f"{name} must not use a placeholder value")
    if len(set(value)) < 4:
        raise ValueError(f"{name} is too weak")


def validate_admin_login_limiter_secret(value: str, *, name: str = "ADMIN_LOGIN_LIMITER_SECRET") -> None:
    """Validate limiter HMAC key material without logging or returning the secret."""
    _validate_secret_material(value, name=name)


def validate_admin_security_settings(settings: Settings) -> None:
    """Validate admin security secrets at startup when auth credentials are present."""
    if not settings.database_url:
        return
    has_admin_credentials = bool(
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    )
    if not has_admin_credentials:
        return
    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous
    if previous:
        validate_admin_login_limiter_secret(
            previous,
            name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret:
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
            )
