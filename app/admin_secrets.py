"""Admin authentication secret validation (fail-fast at startup)."""

from __future__ import annotations

import re

MIN_ADMIN_SECRET_LENGTH = 32

_WEAK_SECRET_VALUES = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "placeholder",
        "replace-me",
        "replace_me",
        "secret",
        "password",
        "test",
        "testing",
        "dev",
        "development",
        "local",
        "admin",
    }
)

_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(changeme|change-me|replace-?me|placeholder|example|your-?secret|insert-?here|todo|fixme)"
)


class AdminSecretValidationError(ValueError):
    """Raised when required admin secret material is missing or too weak."""


def validate_admin_secret(name: str, value: str, *, required: bool = True) -> None:
    """Validate one admin secret env var; never log or echo ``value``."""
    normalized = value.strip()
    if not normalized:
        if required:
            raise AdminSecretValidationError(f"{name} is required")
        return
    if len(normalized) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecretValidationError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    lowered = normalized.lower()
    if lowered in _WEAK_SECRET_VALUES:
        raise AdminSecretValidationError(f"{name} must not be a placeholder value")
    if _PLACEHOLDER_PATTERN.search(normalized):
        raise AdminSecretValidationError(f"{name} must not contain placeholder text")


def validate_admin_login_limiter_secret(
    current: str,
    *,
    previous: str | None = None,
) -> None:
    """Validate current (and optional previous) login limiter key material."""
    validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET", current, required=True)
    if previous is not None and previous.strip():
        validate_admin_secret(
            "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
            previous,
            required=True,
        )
        if previous.strip() == current.strip():
            raise AdminSecretValidationError(
                "ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
