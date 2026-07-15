"""Fail-fast validation for admin security secrets."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_LITERALS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change_me",
        "password",
        "secret",
        "placeholder",
        "admin",
        "test",
        "example",
        "dummy",
        "default",
        "your-secret-here",
        "replace-me",
        "replace_me",
    }
)

_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(changeme|placeholder|example|your[-_]?secret|replace[-_]?me|todo|fixme)"
)


def _normalize_secret_label(label: str) -> str:
    return label.strip() or "ADMIN_LOGIN_LIMITER_SECRET"


def validate_admin_login_limiter_secret(
    secret: str,
    *,
    label: str = "ADMIN_LOGIN_LIMITER_SECRET",
) -> None:
    """Reject missing, weak, or placeholder limiter key material."""
    name = _normalize_secret_label(label)
    value = secret.strip()
    if not value:
        raise ValueError(f"{name} is required when admin authentication is enabled")
    if len(value) < MIN_ADMIN_SECRET_LENGTH:
        raise ValueError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters "
            f"(generate with secrets.token_urlsafe(48))"
        )
    lowered = value.lower()
    if lowered in _WEAK_SECRET_LITERALS:
        raise ValueError(f"{name} must not use a well-known placeholder value")
    if _PLACEHOLDER_PATTERN.search(value):
        raise ValueError(f"{name} must not contain placeholder text")
    if len(set(value)) < 8:
        raise ValueError(f"{name} must contain sufficient entropy")


def validate_admin_security_config(settings: Settings) -> None:
    """Validate admin secrets before serving authenticated routes."""
    if settings.admin_preview_enabled:
        return

    admin_intent = bool(
        settings.database_url
        and settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    )
    if not admin_intent and not settings.admin_auth_configured:
        return

    if settings.admin_auth_configured:
        validate_admin_login_limiter_secret(settings.admin_login_limiter_secret)
        previous = settings.admin_login_limiter_secret_previous.strip()
        if previous:
            validate_admin_login_limiter_secret(
                previous,
                label="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
            )
            if previous == settings.admin_login_limiter_secret:
                raise ValueError(
                    "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                    "ADMIN_LOGIN_LIMITER_SECRET"
                )
        return

    missing: list[str] = []
    if not settings.database_url:
        missing.append("DATABASE_URL")
    if not settings.admin_username:
        missing.append("ADMIN_USERNAME")
    if not settings.admin_password_hash:
        missing.append("ADMIN_PASSWORD_HASH")
    if not settings.admin_session_secret:
        missing.append("ADMIN_SESSION_SECRET")
    if not settings.admin_login_limiter_secret:
        missing.append("ADMIN_LOGIN_LIMITER_SECRET")
    raise ValueError(
        "Admin authentication is partially configured; missing: "
        + ", ".join(missing)
    )
