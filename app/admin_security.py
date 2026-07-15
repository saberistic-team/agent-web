"""Fail-fast validation for admin security secrets."""

from __future__ import annotations

import re

from app.config import Settings

MIN_ADMIN_SECRET_LENGTH = 32

_PLACEHOLDER_PATTERNS = (
    re.compile(r"^changeme$", re.I),
    re.compile(r"^replace[-_]?me$", re.I),
    re.compile(r"^example$", re.I),
    re.compile(r"^placeholder$", re.I),
    re.compile(r"^your[-_]?secret", re.I),
    re.compile(r"^todo$", re.I),
)


def weak_secret_reason(secret: str) -> str | None:
    """Return a human-readable reason when *secret* is unusable, else ``None``."""
    if not secret:
        return "missing"
    if len(secret) < MIN_ADMIN_SECRET_LENGTH:
        return f"shorter than {MIN_ADMIN_SECRET_LENGTH} characters"
    if len(set(secret)) == 1:
        return "repeated single character"
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(secret):
            return "placeholder value"
    return None


def _admin_startup_validation_required(settings: Settings) -> bool:
    """True when admin auth is expected to be operational (or preview-enabled)."""
    core = bool(
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    )
    if settings.admin_preview_mode:
        return core
    return bool(settings.database_configured and core)


def validate_admin_security_config(settings: Settings) -> None:
    """Raise when admin auth is configured but required secrets are weak."""
    if not _admin_startup_validation_required(settings):
        return

    for name, secret in (
        ("ADMIN_SESSION_SECRET", settings.admin_session_secret),
        ("ADMIN_LOGIN_LIMITER_SECRET", settings.admin_login_limiter_secret),
    ):
        reason = weak_secret_reason(secret)
        if reason:
            raise ValueError(
                f"{name} is invalid ({reason}); set a strong environment-specific value"
            )

    previous = settings.admin_login_limiter_previous_secret
    if not previous:
        return

    reason = weak_secret_reason(previous)
    if reason:
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET is invalid "
            f"({reason}); unset or set a strong value"
        )
    if previous == settings.admin_login_limiter_secret:
        raise ValueError(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
            "ADMIN_LOGIN_LIMITER_SECRET during rotation"
        )
