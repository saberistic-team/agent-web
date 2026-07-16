"""Admin security secrets and privacy-preserving login limiter identifiers."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

# Explicit domain separation for limiter key families.
LIMITER_DOMAIN_SOURCE = "src"
LIMITER_DOMAIN_CANDIDATE = "cand"
# Deprecated alias retained for tests and docs migrating from account-only buckets.
LIMITER_DOMAIN_ACCOUNT = LIMITER_DOMAIN_CANDIDATE

MIN_LIMITER_SECRET_BYTES = 32

_PLACEHOLDER_SECRET_RE = re.compile(
    r"^(changeme|placeholder|replace[-_]?me|your[-_]?(secret|key)|"
    r"admin[-_]?(login[-_])?limiter[-_]?(secret|key)|"
    r"test[-_]?(only|secret)|example|dummy|todo)$",
    re.IGNORECASE,
)


class AdminSecurityConfigError(ValueError):
    """Raised when required admin security configuration is missing or weak."""


def _secret_byte_length(secret: str) -> int:
    return len(secret.encode("utf-8"))


def validate_limiter_secret(
    value: str,
    *,
    env_name: str,
    required: bool = True,
) -> None:
    """Validate one login-limiter secret for strength and placeholder rejection."""
    normalized = value.strip()
    if not normalized:
        if required:
            raise AdminSecurityConfigError(f"{env_name} is required")
        return
    if _PLACEHOLDER_SECRET_RE.match(normalized):
        raise AdminSecurityConfigError(f"{env_name} must not use placeholder values")
    if _secret_byte_length(normalized) < MIN_LIMITER_SECRET_BYTES:
        raise AdminSecurityConfigError(
            f"{env_name} must be at least {MIN_LIMITER_SECRET_BYTES} bytes"
        )


def validate_admin_security_config(settings: Settings) -> None:
    """Fail-fast validation of admin limiter secrets at application startup."""
    if not settings.database_url:
        return
    if not (
        settings.admin_username
        and settings.admin_password_hash
        and settings.admin_session_secret
    ):
        return

    validate_limiter_secret(
        settings.admin_login_limiter_secret,
        env_name="ADMIN_LOGIN_LIMITER_SECRET",
        required=True,
    )
    previous = settings.admin_login_limiter_previous_secret.strip()
    if previous:
        validate_limiter_secret(
            previous,
            env_name="ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET",
            required=True,
        )
        if hmac.compare_digest(previous, settings.admin_login_limiter_secret.strip()):
            raise AdminSecurityConfigError(
                "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must differ from "
                "ADMIN_LOGIN_LIMITER_SECRET"
            )


def digest_limiter_key(*, domain: str, material: str, secret: str) -> str:
    """Return a fixed-length HMAC-SHA256 hex digest with domain separation."""
    payload = f"{domain}:{material}"
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def plain_sha256_limiter_key(domain: str, material: str) -> str:
    """Legacy unkeyed digest retained for tests proving HMAC differs from SHA-256."""
    payload = f"{domain}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_limiter_digests(left: str, right: str) -> bool:
    """Constant-time comparison for application-level digest checks."""
    return hmac.compare_digest(left, right)
