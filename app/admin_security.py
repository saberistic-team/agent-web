"""Admin authentication security primitives and configuration validation."""

from __future__ import annotations

import hashlib
import hmac
import re

from app.config import Settings

LIMITER_DOMAIN_SOURCE = "src"
LIMITER_DOMAIN_ACCOUNT = "acct"

_MIN_SECRET_BYTES = 32
_WEAK_SECRET_VALUES = frozenset(
    {
        "changeme",
        "change-me",
        "placeholder",
        "secret",
        "password",
        "admin",
        "test",
        "dev",
        "development",
        "production",
        "example",
        "dummy",
        "default",
    }
)
_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(changeme|placeholder|example|dummy|your[_-]?secret|insert[_-]?here|todo|fixme)"
)


class AdminSecurityConfigError(ValueError):
    """Raised when admin security configuration is invalid."""


def validate_admin_security_config(settings: Settings) -> None:
    """Fail fast when admin auth secrets are missing or weak."""
    if not settings.admin_auth_configured:
        return
    errors: list[str] = []
    session_err = _validate_secret("ADMIN_SESSION_SECRET", settings.admin_session_secret)
    if session_err:
        errors.append(session_err)
    limiter_err = _validate_secret(
        "ADMIN_LOGIN_LIMITER_SECRET",
        settings.admin_login_limiter_secret,
    )
    if limiter_err:
        errors.append(limiter_err)
    previous = settings.admin_login_limiter_previous_secret
    if previous:
        previous_err = _validate_secret(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
            previous,
        )
        if previous_err:
            errors.append(previous_err)
        if hmac.compare_digest(previous, settings.admin_login_limiter_secret):
            errors.append(
                "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )
    if errors:
        raise AdminSecurityConfigError("; ".join(errors))


def _validate_secret(env_name: str, value: str) -> str | None:
    if not value:
        return f"{env_name} is required when admin authentication is configured"
    if len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
        return f"{env_name} must be at least {_MIN_SECRET_BYTES} bytes"
    normalized = value.strip().lower()
    if normalized in _WEAK_SECRET_VALUES:
        return f"{env_name} must not be a placeholder or weak value"
    if _PLACEHOLDER_PATTERN.search(value):
        return f"{env_name} must not contain placeholder text"
    return None


def limiter_key_payload(domain: str, material: str) -> bytes:
    """Return domain-separated UTF-8 payload for limiter HMAC input."""
    return f"{domain}:{material.strip().lower()}".encode("utf-8")


def digest_limiter_key(secret: str, domain: str, material: str) -> str:
    """Keyed HMAC-SHA256 identifier; fixed 64-char hex for TEXT primary keys."""
    return hmac.new(
        secret.encode("utf-8"),
        limiter_key_payload(domain, material),
        hashlib.sha256,
    ).hexdigest()


def plain_sha256_limiter_key(domain: str, material: str) -> str:
    """Legacy unkeyed digest retained for tests and migration documentation."""
    return hashlib.sha256(limiter_key_payload(domain, material)).hexdigest()


def compare_limiter_keys(left: str, right: str) -> bool:
    """Constant-time comparison for application-level limiter key checks."""
    return hmac.compare_digest(left, right)
