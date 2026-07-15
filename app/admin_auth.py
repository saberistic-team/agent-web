"""Single-operator admin authentication with server-side sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from fastapi import Request
from fastapi.responses import Response

from app import db
from app.config import Settings

SESSION_COOKIE_NAME = "admin_session"
LOGIN_FLOW_COOKIE_NAME = "admin_login_flow"
CSRF_FORM_FIELD = "csrf_token"
CSRF_MAX_AGE_SECONDS = 900
# Authenticated session CSRF lifetime matches ``ADMIN_SESSION_TTL_SECONDS`` (see
# ``derive_session_csrf_token``). Tokens are not rotated on navigation so forms
# stay valid across multiple tabs until the session expires, is revoked, or is
# replaced at login.
# Retention after ``expires_at`` before deleting never-consumed flows.
LOGIN_FLOW_EXPIRED_RETENTION_SECONDS = CSRF_MAX_AGE_SECONDS * 2
# Retention after ``consumed_at`` before deleting one-time-used flows.
LOGIN_FLOW_CONSUMED_RETENTION_SECONDS = CSRF_MAX_AGE_SECONDS
LOGIN_FLOW_CLEANUP_BATCH_SIZE = 100
INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."
INVALID_REQUEST_MESSAGE = "Invalid request."
LOGIN_THROTTLED_MESSAGE = "Too many login attempts. Try again later."

# Documented upper bounds enforced before hashing, normalization, or Argon2 work.
LOGIN_USERNAME_MAX_LENGTH = 256
LOGIN_PASSWORD_MAX_LENGTH = 256
LOGIN_CSRF_MAX_LENGTH = 128
LOGIN_FLOW_TOKEN_MAX_LENGTH = 128

# Privacy-preserving limiter key namespace (pepper uses ADMIN_SESSION_SECRET).
_LIMITER_KEY_VERSION = "admin-login-limiter-v1"

# Conservative in-memory fallback when shared Postgres limiter storage is unavailable.
_FALLBACK_RATE_LIMIT = 2
_FALLBACK_WINDOW_SECONDS = 60

_password_hasher = PasswordHasher()
_fallback_lock = Lock()
_fallback_attempts: dict[str, tuple[int, float, float | None]] = {}
_logger = logging.getLogger(__name__)


class AdminLoginRequired(Exception):
    """Raised when a protected admin route needs authentication."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


@dataclass(frozen=True)
class AdminSession:
    """Validated server-side admin session."""

    id: int
    admin_username: str
    token_hash: str
    csrf_token_hash: str | None
    expires_at: datetime


@dataclass(frozen=True)
class AdminLoginAdmission:
    """Result of an atomic shared-store login admission decision."""

    admitted: bool
    throttled: bool
    newly_locked: bool
    store_unavailable: bool = False


def preview_admin_session(settings: Settings) -> AdminSession:
    """Synthetic session for ADMIN_PREVIEW_MODE (local/CI screenshots only)."""
    return AdminSession(
        id=0,
        admin_username=settings.admin_username or "preview-operator",
        token_hash="preview",
        csrf_token_hash=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def reset_login_rate_limiter() -> None:
    """Clear fallback login attempt counters (tests only)."""
    with _fallback_lock:
        _fallback_attempts.clear()


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expires_at(settings: Settings) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=settings.admin_session_ttl_seconds)


def cookie_secure(settings: Settings) -> bool:
    return settings.base_url.startswith("https://")


def set_session_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=cookie_secure(settings),
        samesite="strict",
        path="/admin",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/admin",
        secure=cookie_secure(settings),
        httponly=True,
        samesite="strict",
    )


def generate_csrf_value() -> str:
    """Return a cryptographically random CSRF synchronizer token."""
    return secrets.token_urlsafe(32)


def hash_csrf_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def verify_csrf_value(raw_token: str, stored_hash: str | None) -> bool:
    """Constant-time comparison of a submitted CSRF token against a stored hash."""
    if not raw_token or not stored_hash:
        return False
    expected = hash_csrf_token(raw_token)
    return hmac.compare_digest(expected, stored_hash)


def derive_session_csrf_token(raw_session_token: str, settings: Settings) -> str:
    """Return the stable synchronizer token bound to an authenticated session cookie.

    The token is derived with HMAC-SHA256 over the raw session token using
    ``ADMIN_SESSION_SECRET``. It is stored only in HTML forms (never logged) and
    validated on each state-changing request. Lifetime equals the session TTL;
    replacing or revoking the session cookie invalidates the token.
    """
    secret = settings.admin_session_secret
    if not secret:
        raise ValueError("admin_session_secret is required for session CSRF")
    digest = hmac.new(
        secret.encode("utf-8"),
        raw_session_token.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def session_csrf_for_request(request: Request, settings: Settings) -> str:
    """Derive the session-bound CSRF token for the active admin session cookie."""
    raw_session_token = read_session_token(request)
    if raw_session_token is None:
        return ""
    return derive_session_csrf_token(raw_session_token, settings)


def verify_session_csrf_request(
    request: Request,
    submitted_csrf_token: str,
    settings: Settings,
) -> bool:
    """Validate a submitted CSRF token against the active session cookie."""
    raw_session_token = read_session_token(request)
    if not raw_session_token or not submitted_csrf_token:
        return False
    expected = derive_session_csrf_token(raw_session_token, settings)
    return hmac.compare_digest(submitted_csrf_token, expected)


def login_flow_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=CSRF_MAX_AGE_SECONDS)


def set_login_flow_cookie(response: Response, raw_flow_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=LOGIN_FLOW_COOKIE_NAME,
        value=raw_flow_token,
        max_age=CSRF_MAX_AGE_SECONDS,
        httponly=True,
        secure=cookie_secure(settings),
        samesite="strict",
        path="/admin",
    )


def clear_login_flow_cookie(response: Response, settings: Settings) -> None:
    """Expire the pre-auth flow cookie after successful login only.

    Failed login responses mint a replacement flow via ``set_login_flow_cookie``;
    calling this on those responses would delete the cookie the form's CSRF token
    depends on.
    """
    response.delete_cookie(
        key=LOGIN_FLOW_COOKIE_NAME,
        path="/admin",
        secure=cookie_secure(settings),
        httponly=True,
        samesite="strict",
    )


def read_login_flow_token(request: Request) -> str | None:
    token = request.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    if not token:
        return None
    return token.strip() or None


def client_ip(request: Request, settings: Settings) -> str:
    """Resolve the client source IP for rate limiting.

    Forwarding headers are honored only when ``ADMIN_TRUST_PROXY_HEADERS`` is
    enabled (e.g. behind Render's load balancer). Otherwise the direct peer
    address is used so clients cannot spoof ``X-Forwarded-For``.

    IPv4 and IPv6 addresses are used as returned by the ASGI server or the
    left-most ``X-Forwarded-For`` value when trusted. Missing peer information
    maps to the stable sentinel ``unknown``.
    """
    if settings.admin_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def login_form_inputs_within_bounds(
    *,
    username: str,
    password: str,
    csrf_token: str,
    login_flow_token: str | None,
) -> bool:
    """Return False when any submitted login value exceeds documented limits."""
    if len(username) > LOGIN_USERNAME_MAX_LENGTH:
        return False
    if len(password) > LOGIN_PASSWORD_MAX_LENGTH:
        return False
    if len(csrf_token) > LOGIN_CSRF_MAX_LENGTH:
        return False
    if login_flow_token is not None and len(login_flow_token) > LOGIN_FLOW_TOKEN_MAX_LENGTH:
        return False
    return True


def _limiter_pepper(settings: Settings) -> str:
    return settings.admin_session_secret or ""


def build_admin_login_source_limiter_key(client_source: str, settings: Settings) -> str:
    """Source-wide bucket keyed by resolved client source (username rotation safe)."""
    material = f"{_LIMITER_KEY_VERSION}:source:{client_source}:{_limiter_pepper(settings)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_admin_login_account_limiter_key(settings: Settings) -> str:
    """Account-wide bucket for the configured admin username across sources."""
    normalized = settings.admin_username.strip().lower()
    material = f"{_LIMITER_KEY_VERSION}:account:{normalized}:{_limiter_pepper(settings)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_rate_limit_key(username: str, client_source: str) -> str:
    """Legacy combined key helper retained for tests and documentation parity."""
    normalized_username = username.strip().lower()
    material = f"{normalized_username}:{client_source}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def submitted_username_targets_configured_admin(username: str, settings: Settings) -> bool:
    configured = settings.admin_username.strip().lower()
    submitted = username.strip().lower()
    if not configured or not submitted:
        return False
    return secrets.compare_digest(submitted, configured)


def admin_login_limiter_keys(username: str, client_source: str, settings: Settings) -> tuple[str, ...]:
    """Return ordered limiter buckets consulted for one login admission."""
    source_key = build_admin_login_source_limiter_key(client_source, settings)
    if submitted_username_targets_configured_admin(username, settings):
        return (source_key, build_admin_login_account_limiter_key(settings))
    return (source_key,)


def _fallback_state(limiter_key: str, now: float) -> tuple[int, float, float | None]:
    with _fallback_lock:
        entry = _fallback_attempts.get(limiter_key)
        if entry is None:
            return 0, now, None
        count, window_start, locked_until = entry
        if now - window_start >= _FALLBACK_WINDOW_SECONDS:
            del _fallback_attempts[limiter_key]
            return 0, now, None
        return count, window_start, locked_until


def _fallback_admit(limiter_keys: tuple[str, ...]) -> AdminLoginAdmission:
    now = time.time()
    snapshots: dict[str, tuple[int, float, float | None]] = {}
    for key in limiter_keys:
        count, window_start, locked_until = _fallback_state(key, now)
        if locked_until is not None and locked_until > now:
            _logger.info(
                "Admin login throttled via fallback limiter",
                extra={"bucket_count": len(limiter_keys), "outcome": "throttled"},
            )
            return AdminLoginAdmission(
                admitted=False,
                throttled=True,
                newly_locked=False,
                store_unavailable=True,
            )
        snapshots[key] = (count, window_start, locked_until)

    newly_locked = False
    with _fallback_lock:
        for key in limiter_keys:
            count, window_start, locked_until = snapshots[key]
            if count >= _FALLBACK_RATE_LIMIT:
                lock_until = now + _FALLBACK_WINDOW_SECONDS
                _fallback_attempts[key] = (count, window_start, lock_until)
                newly_locked = True
                _logger.info(
                    "Admin login throttled via fallback limiter",
                    extra={"bucket_count": len(limiter_keys), "outcome": "lockout"},
                )
                return AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    newly_locked=newly_locked,
                    store_unavailable=True,
                )
            _fallback_attempts[key] = (count + 1, window_start, locked_until)
    _logger.info(
        "Admin login admitted via fallback limiter",
        extra={"bucket_count": len(limiter_keys), "outcome": "admitted"},
    )
    return AdminLoginAdmission(
        admitted=True,
        throttled=False,
        newly_locked=False,
        store_unavailable=True,
    )


def _clear_fallback_keys(limiter_keys: tuple[str, ...]) -> None:
    with _fallback_lock:
        for key in limiter_keys:
            _fallback_attempts.pop(key, None)


def admit_admin_login(
    request: Request,
    settings: Settings,
    *,
    username: str,
) -> AdminLoginAdmission:
    """Atomically reserve one login attempt across shared limiter buckets."""
    client_source = client_ip(request, settings)
    limiter_keys = admin_login_limiter_keys(username, client_source, settings)
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            admitted, newly_locked = db.admit_admin_login_attempt(
                conn,
                limiter_keys=limiter_keys,
                now=now,
                rate_limit=settings.admin_login_rate_limit,
                window_seconds=settings.admin_login_rate_window_seconds,
                lockout_seconds=settings.admin_login_lockout_seconds,
            )
            db.cleanup_expired_admin_login_rate_limits(
                conn,
                now=now,
                window_seconds=settings.admin_login_rate_window_seconds,
                lockout_seconds=settings.admin_login_lockout_seconds,
            )
    except Exception:
        _logger.warning(
            "Admin login rate limiter unavailable; using conservative fallback",
            exc_info=True,
            extra={"bucket_count": len(limiter_keys), "outcome": "store_failure"},
        )
        return _fallback_admit(limiter_keys)

    if admitted:
        _logger.info(
            "Admin login admitted",
            extra={"bucket_count": len(limiter_keys), "outcome": "admitted"},
        )
        return AdminLoginAdmission(
            admitted=True,
            throttled=False,
            newly_locked=newly_locked,
        )

    _logger.info(
        "Admin login throttled",
        extra={
            "bucket_count": len(limiter_keys),
            "outcome": "lockout" if newly_locked else "throttled",
        },
    )
    return AdminLoginAdmission(
        admitted=False,
        throttled=True,
        newly_locked=newly_locked,
    )


def clear_admin_login_account_limit(request: Request, settings: Settings) -> None:
    """Clear the configured account bucket after successful login only."""
    account_key = build_admin_login_account_limiter_key(settings)
    _clear_fallback_keys((account_key,))
    try:
        with db.db_connection(settings.database_url) as conn:
            db.clear_admin_login_rate_limit(conn, limiter_key=account_key)
    except Exception:
        _logger.warning(
            "Admin login account limiter unavailable; cleared fallback only",
            exc_info=True,
        )


def release_admin_login_admission(
    request: Request,
    settings: Settings,
    *,
    username: str,
) -> None:
    """Release one admitted reservation after successful password verification."""
    client_source = client_ip(request, settings)
    limiter_keys = admin_login_limiter_keys(username, client_source, settings)
    now = datetime.now(timezone.utc)
    with _fallback_lock:
        for key in limiter_keys:
            entry = _fallback_attempts.get(key)
            if entry is None:
                continue
            count, window_start, locked_until = entry
            count = max(0, count - 1)
            if count == 0:
                _fallback_attempts.pop(key, None)
            else:
                _fallback_attempts[key] = (count, window_start, locked_until)
    try:
        with db.db_connection(settings.database_url) as conn:
            db.release_admin_login_admission(
                conn,
                limiter_keys=limiter_keys,
                now=now,
                rate_limit=settings.admin_login_rate_limit,
            )
    except Exception:
        _logger.warning(
            "Admin login admission release unavailable; adjusted fallback only",
            exc_info=True,
        )


def verify_admin_credentials(username: str, password: str, settings: Settings) -> bool:
    """Verify credentials without revealing whether the username exists."""
    username_ok = secrets.compare_digest(username, settings.admin_username)
    try:
        _password_hasher.verify(settings.admin_password_hash, password)
        password_ok = True
    except (VerifyMismatchError, InvalidHash):
        password_ok = False
    return username_ok and password_ok


def read_session_token(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return token.strip() or None


def session_from_row(row: dict[str, Any]) -> AdminSession:
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return AdminSession(
        id=int(row["id"]),
        admin_username=str(row["admin_username"]),
        token_hash=str(row["token_hash"]),
        csrf_token_hash=row.get("csrf_token_hash"),
        expires_at=expires_at,
    )


def login_redirect_url(next_path: str | None) -> str:
    if next_path and next_path.startswith("/admin") and not next_path.startswith("/admin/login"):
        return f"/admin/login?next={quote(next_path, safe='')}"
    return "/admin/login"


def safe_admin_next_path(next_path: str | None) -> str:
    if next_path and next_path.startswith("/admin") and not next_path.startswith("/admin/login"):
        return next_path
    return "/admin"
