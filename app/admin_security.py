"""Admin security secret validation and privacy-preserving limiter identifiers."""

from __future__ import annotations

import hashlib
import hmac

from app.config import Settings

LIMITER_DOMAIN_SOURCE = "src"
LIMITER_DOMAIN_ACCOUNT = "acct"

MIN_ADMIN_SECRET_LENGTH = 32

_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "placeholder",
        "secret",
        "password",
        "admin",
        "test",
        "dev",
        "example",
    }
)
_PLACEHOLDER_PREFIXES = ("changeme", "change-me", "placeholder", "your-secret")


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security secrets are missing or weak."""


def _validate_single_secret(name: str, value: str) -> None:
    stripped = value.strip()
    if not stripped:
        raise AdminSecurityConfigError(f"{name} is required when admin auth is configured")
    if len(stripped) < MIN_ADMIN_SECRET_LENGTH:
        raise AdminSecurityConfigError(
            f"{name} must be at least {MIN_ADMIN_SECRET_LENGTH} characters"
        )
    normalized = stripped.lower()
    if normalized in _PLACEHOLDER_SECRETS:
        raise AdminSecurityConfigError(f"{name} must not be a placeholder value")
    if any(normalized.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES):
        raise AdminSecurityConfigError(f"{name} must not be a placeholder value")
    if len(set(stripped)) == 1:
        raise AdminSecurityConfigError(f"{name} is too weak")


def validate_admin_security_config(settings: Settings) -> None:
    """Fail fast when admin auth is partially configured with weak secrets."""
    if not settings.admin_username:
        return
    _validate_single_secret("ADMIN_SESSION_SECRET", settings.admin_session_secret)
    _validate_single_secret("ADMIN_LOGIN_LIMITER_SECRET", settings.admin_login_limiter_secret)
    previous = settings.admin_login_limiter_previous_secret.strip()
    if previous:
        _validate_single_secret("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", previous)
        if hmac.compare_digest(previous, settings.admin_login_limiter_secret.strip()):
            raise AdminSecurityConfigError(
                "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET during rotation"
            )


def digest_limiter_key(*, secret: str, domain: str, material: str) -> str:
    """Return a fixed-length HMAC-SHA256 identifier for one limiter bucket."""
    payload = f"{domain}:{material}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def unkeyed_sha256_limiter_identifier(domain: str, material: str) -> str:
    """Pre-#242 SHA-256 construction retained for regression tests only."""
    payload = f"{domain}:{material}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def limiter_keys_equal(left: str, right: str) -> bool:
    """Constant-time comparison of persisted limiter identifiers."""
    return hmac.compare_digest(left, right)
