"""Admin authentication secrets and privacy-preserving limiter identifiers."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Final

from app.config import Settings

MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES: Final = 32

LIMITER_DOMAIN_SOURCE: Final = "admin-login-limiter:src:v1"
LIMITER_DOMAIN_ACCOUNT: Final = "admin-login-limiter:acct:v1"

_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)^(changeme|change[_-]?me|replace[_-]?me|placeholder|your[_-]?secret|"
    r"admin[_-]?login[_-]?limiter[_-]?secret|example|todo|fixme|secret)$"
)


def validate_admin_login_limiter_secret(value: str, *, env_name: str) -> bytes:
    """Return UTF-8 secret bytes or raise ValueError for weak or missing material."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{env_name} is required when admin authentication is enabled")
    if _PLACEHOLDER_PATTERN.match(stripped):
        raise ValueError(f"{env_name} must not be a placeholder value")
    encoded = stripped.encode("utf-8")
    if len(encoded) < MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES:
        raise ValueError(
            f"{env_name} must be at least {MIN_ADMIN_LOGIN_LIMITER_SECRET_BYTES} bytes"
        )
    return encoded


def admin_login_limiter_secrets(settings: Settings) -> tuple[bytes, ...]:
    """Return the active limiter secret and optional previous secret for rotation."""
    current = validate_admin_login_limiter_secret(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
    )
    secrets: list[bytes] = [current]
    previous = settings.admin_login_limiter_previous_secret.strip()
    if previous:
        previous_bytes = validate_admin_login_limiter_secret(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
        )
        if previous_bytes != current:
            secrets.append(previous_bytes)
    return tuple(secrets)


def digest_limiter_identifier(domain: str, material: str, secret: bytes) -> str:
    """Return a keyed HMAC-SHA256 identifier with explicit domain separation."""
    payload = f"{domain}:{material}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def compare_limiter_identifiers(left: str, right: str) -> bool:
    """Constant-time comparison for persisted limiter identifiers."""
    return hmac.compare_digest(left, right)


def validate_admin_auth_security_settings(settings: Settings) -> None:
    """Fail fast on weak admin security configuration at process startup."""
    if not settings.admin_auth_configured:
        return
    admin_login_limiter_secrets(settings)
