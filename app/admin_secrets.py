"""Admin security secret validation (fail-fast at startup)."""

from __future__ import annotations

from app.config import Settings

MIN_ADMIN_SECRET_BYTES = 32

_WEAK_SECRET_MARKERS = (
    "changeme",
    "change-me",
    "placeholder",
    "example",
    "your-secret",
    "dev-only",
    "insert-secret",
    "replace-me",
)


def _validate_secret_material(value: str, *, field: str) -> None:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required when admin authentication is configured")
    if len(stripped.encode("utf-8")) < MIN_ADMIN_SECRET_BYTES:
        raise ValueError(
            f"{field} must be at least {MIN_ADMIN_SECRET_BYTES} bytes of key material"
        )
    lowered = stripped.lower()
    for marker in _WEAK_SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{field} must not contain placeholder or weak material")


def validate_admin_security_config(settings: Settings) -> None:
    """Fail fast when admin auth is enabled with weak or missing limiter secrets."""
    if not settings.database_url or not settings.admin_username:
        return
    if not settings.admin_password_hash or not settings.admin_session_secret:
        return
    _validate_secret_material(
        settings.admin_login_limiter_secret,
        field="ADMIN_LOGIN_LIMITER_SECRET",
    )
    previous = settings.admin_login_limiter_secret_previous.strip()
    if previous:
        _validate_secret_material(
            previous,
            field="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )
        if previous == settings.admin_login_limiter_secret.strip():
            raise ValueError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
