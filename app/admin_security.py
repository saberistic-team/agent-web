"""Admin security secret validation (fail-fast at startup)."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_BYTES = 32

_WEAK_EXACT_SECRETS = frozenset(
    {
        "changeme",
        "password",
        "secret",
        "placeholder",
        "example",
        "test",
        "admin",
    }
)


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    field_name: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, malformed, or placeholder limiter key material."""
    if not secret:
        raise ValueError(f"{field_name} is required")
    if secret != secret.strip():
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if len(secret.encode("utf-8")) < MIN_ADMIN_SECRET_BYTES:
        raise ValueError(
            f"{field_name} must be at least {MIN_ADMIN_SECRET_BYTES} random bytes"
        )
    if secret.lower() in _WEAK_EXACT_SECRETS:
        raise ValueError(f"{field_name} must not use placeholder or dictionary values")
    if len(set(secret)) <= 2:
        raise ValueError(f"{field_name} must not use low-entropy repeated material")


def validate_admin_auth_secrets(settings: Settings) -> None:
    """Validate admin security secrets before serving authenticated routes."""
    validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_secret_previous
    if previous:
        validate_admin_login_limiter_secret(
            previous,
            field_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret:
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
