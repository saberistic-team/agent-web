"""Admin security secret validation (fail-fast at startup)."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "placeholder",
        "replace-me",
        "secret",
        "admin",
        "password",
        "test",
        "dev",
        "development",
        "local",
    }
)

_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(changeme|placeholder|replace[_-]?me|your[_-]?secret|insert[_-]?secret|todo|fixme|xxx+)"
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security secrets are missing or weak."""


def _normalize_secret(value: str) -> str:
    return value.strip()


def validate_admin_secret(
    value: str,
    *,
    field_name: str,
    min_length: int = MIN_ADMIN_SECRET_LENGTH,
) -> None:
    """Reject missing, weak, placeholder, or malformed admin secret material."""
    normalized = _normalize_secret(value)
    if not normalized:
        raise AdminSecurityConfigError(f"{field_name} is required")
    if len(normalized) < min_length:
        raise AdminSecurityConfigError(
            f"{field_name} must be at least {min_length} characters"
        )
    lowered = normalized.lower()
    if lowered in _WEAK_SECRET_LITERALS:
        raise AdminSecurityConfigError(f"{field_name} uses a disallowed placeholder value")
    if _PLACEHOLDER_PATTERN.search(normalized):
        raise AdminSecurityConfigError(f"{field_name} uses a disallowed placeholder value")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AdminSecurityConfigError(f"{field_name} must be valid UTF-8") from exc
    if len(set(normalized)) < 4:
        raise AdminSecurityConfigError(f"{field_name} is too weak")


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin security secrets when authentication is enabled."""
    if not settings.admin_auth_configured:
        return

    validate_admin_secret(settings.admin_session_secret, field_name="ADMIN_SESSION_SECRET")

    if not settings.database_url:
        return

    validate_admin_secret(
        settings.admin_login_limiter_secret,
        field_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = _normalize_secret(settings.admin_login_limiter_secret_previous)
    if previous:
        validate_admin_secret(
            previous,
            field_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == _normalize_secret(settings.admin_login_limiter_secret):
            raise AdminSecurityConfigError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from ADMIN_LOGIN_LIMITER_SECRET"
            )
