"""Startup validation for admin authentication security configuration."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_BYTES = 32

_PLACEHOLDER_SUBSTRINGS = (
    "changeme",
    "change-me",
    "placeholder",
    "replace-me",
    "your-secret",
    "example-secret",
    "set-me",
    "todo",
)


def validate_admin_secret_value(value: str, *, env_name: str) -> None:
    """Fail fast when admin secret material is missing, weak, or placeholder."""
    if not value or not value.strip():
        raise ValueError(f"{env_name} is required when admin authentication is configured")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{env_name} must be valid UTF-8") from exc
    if len(encoded) < MIN_ADMIN_SECRET_BYTES:
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_SECRET_BYTES} bytes of key material"
        )
    lowered = value.strip().lower()
    for fragment in _PLACEHOLDER_SUBSTRINGS:
        if fragment in lowered:
            raise ValueError(f"{env_name} must not use placeholder key material")


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin security secrets before serving authenticated routes."""
    validate_admin_secret_value(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        validate_admin_secret_value(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret:
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
