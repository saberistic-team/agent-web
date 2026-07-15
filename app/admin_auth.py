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
from app.admin_client_source import resolve_admin_login_client_source
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
LOGIN_USERNAME_MAX_LENGTH = 256
LOGIN_PASSWORD_MAX_LENGTH = 512
LOGIN_CSRF_MAX_LENGTH = 256
LOGIN_FLOW_TOKEN_MAX_LENGTH = 512
LOGIN_NEXT_MAX_LENGTH = 2048

# Conservative in-memory fallback when shared Postgres limiter storage is unavailable.
_FALLBACK_RATE_LIMIT = 2
_FALLBACK_WINDOW_SECONDS = 60

_password_hasher = PasswordHasher()
_fallback_lock = Lock()
_fallback_attempts: dict[str, tuple[int, float]] = {}
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
class LoginAdmissionResult:
    """Shared-store admission decision for one login POST."""

    admitted: bool
    throttled: bool
    already_locked: bool
    lockout_transition: bool
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

    Delegates to :func:`app.admin_client_source.resolve_admin_login_client_source`
    which enforces the configured trusted-proxy boundary before honoring
    forwarding headers. Resolved values are normalized IPv4/IPv6 strings used
    only as keyed digests; raw addresses and header chains are never logged or
    persisted.
    """
    return resolve_admin_login_client_source(request, settings).source


def _digest_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_source_rate_limit_key(client_source: str) -> str:
    """Source-wide bucket keyed by resolved client source (privacy-preserving)."""
    normalized_source = client_source.strip().lower()
    return _digest_limiter_key("src", normalized_source)


def build_account_rate_limit_key(admin_username: str) -> str:
    """Account-wide bucket for the configured admin username."""
    normalized_username = admin_username.strip().lower()
    return _digest_limiter_key("acct", normalized_username)


def build_rate_limit_key(username: str, client_source: str) -> str:
    """Deprecated composite key kept for tests migrating to dual-bucket strategy."""
    normalized_username = username.strip().lower()
    material = f"{normalized_username}:{client_source.strip().lower()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def login_limiter_keys(
    *,
    submitted_username: str,
    client_source: str,
    configured_admin_username: str,
) -> tuple[str, ...]:
    """Return the shared limiter buckets consulted for one login attempt."""
    keys = [build_source_rate_limit_key(client_source)]
    normalized_submitted = submitted_username.strip().lower()
    normalized_configured = configured_admin_username.strip().lower()
    if normalized_configured and normalized_submitted == normalized_configured:
        keys.append(build_account_rate_limit_key(configured_admin_username))
    return tuple(keys)


def _is_fallback_throttled(limiter_keys: tuple[str, ...]) -> bool:
    now = time.time()
    with _fallback_lock:
        for limiter_key in limiter_keys:
            entry = _fallback_attempts.get(limiter_key)
            if entry is None:
                continue
            count, window_start = entry
            if now - window_start >= _FALLBACK_WINDOW_SECONDS:
                del _fallback_attempts[limiter_key]
                continue
            if count >= _FALLBACK_RATE_LIMIT:
                return True
    return False


def _record_fallback_admission(limiter_keys: tuple[str, ...]) -> LoginAdmissionResult:
    now = time.time()
    lockout_transition = False
    with _fallback_lock:
        for limiter_key in limiter_keys:
            count, window_start = _fallback_attempts.get(limiter_key, (0, now))
            if now - window_start >= _FALLBACK_WINDOW_SECONDS:
                count = 0
                window_start = now
            prior_count = count
            count += 1
            _fallback_attempts[limiter_key] = (count, window_start)
            if prior_count < _FALLBACK_RATE_LIMIT <= count:
                lockout_transition = True
    return LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=lockout_transition,
    )


def _clear_fallback_failures(limiter_keys: tuple[str, ...]) -> None:
    with _fallback_lock:
        for limiter_key in limiter_keys:
            _fallback_attempts.pop(limiter_key, None)


def _release_fallback_admission(limiter_key: str) -> None:
    with _fallback_lock:
        entry = _fallback_attempts.get(limiter_key)
        if entry is None:
            return
        count, window_start = entry
        _fallback_attempts[limiter_key] = (max(count - 1, 0), window_start)


def login_form_inputs_valid(
    *,
    username: str,
    password: str,
    csrf_token: str,
    login_flow_token: str | None,
    next_path: str | None = None,
) -> bool:
    """Reject oversized login POST fields before hashing, storage, or verification."""
    if not username or len(username) > LOGIN_USERNAME_MAX_LENGTH:
        return False
    if not password or len(password) > LOGIN_PASSWORD_MAX_LENGTH:
        return False
    if not csrf_token or len(csrf_token) > LOGIN_CSRF_MAX_LENGTH:
        return False
    if login_flow_token is not None and len(login_flow_token) > LOGIN_FLOW_TOKEN_MAX_LENGTH:
        return False
    if next_path is not None and len(next_path) > LOGIN_NEXT_MAX_LENGTH:
        return False
    return True


def try_admit_login_attempt(
    request: Request,
    settings: Settings,
    *,
    username: str = "",
) -> LoginAdmissionResult:
    """Atomically reserve shared limiter capacity before password verification."""
    source = client_ip(request, settings)
    limiter_keys = login_limiter_keys(
        submitted_username=username,
        client_source=source,
        configured_admin_username=settings.admin_username,
    )
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            admission = db.try_admit_admin_login(
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
            extra={"limiter_key_count": len(limiter_keys)},
            exc_info=True,
        )
        if _is_fallback_throttled(limiter_keys):
            return LoginAdmissionResult(
                admitted=False,
                throttled=True,
                already_locked=True,
                lockout_transition=False,
                store_unavailable=True,
            )
        fallback = _record_fallback_admission(limiter_keys)
        return LoginAdmissionResult(
            admitted=fallback.admitted,
            throttled=fallback.throttled,
            already_locked=fallback.already_locked,
            lockout_transition=fallback.lockout_transition,
            store_unavailable=True,
        )

    if admission.admitted:
        _logger.info(
            "Admin login attempt admitted",
            extra={
                "limiter_key_count": len(limiter_keys),
                "lockout_transition": admission.lockout_transition,
            },
        )
    elif admission.already_locked:
        _logger.info(
            "Admin login attempt throttled",
            extra={
                "limiter_key_count": len(limiter_keys),
                "already_locked": True,
            },
        )
    return LoginAdmissionResult(
        admitted=admission.admitted,
        throttled=admission.throttled,
        already_locked=admission.already_locked,
        lockout_transition=admission.lockout_transition,
    )


def is_login_throttled(request: Request, settings: Settings, *, username: str = "") -> bool:
    """Return whether login attempts are currently blocked (read-only helper)."""
    source = client_ip(request, settings)
    limiter_keys = login_limiter_keys(
        submitted_username=username,
        client_source=source,
        configured_admin_username=settings.admin_username,
    )
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            return any(
                db.is_admin_login_throttled(conn, limiter_key=key, now=now)
                for key in limiter_keys
            )
    except Exception:
        _logger.warning(
            "Admin login rate limiter unavailable; using conservative fallback",
            exc_info=True,
        )
        return _is_fallback_throttled(limiter_keys)


def record_failed_login(request: Request, settings: Settings, *, username: str = "") -> None:
    """Deprecated: failures are counted during :func:`try_admit_login_attempt`."""
    _ = (request, settings, username)


def finalize_successful_login(request: Request, settings: Settings, *, username: str = "") -> None:
    """Clear account bucket state and release the current source admission reservation."""
    _ = username
    source = client_ip(request, settings)
    source_key = build_source_rate_limit_key(source)
    account_keys = (
        (build_account_rate_limit_key(settings.admin_username),)
        if settings.admin_username.strip()
        else ()
    )
    _clear_fallback_failures(account_keys)
    _release_fallback_admission(source_key)
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            if account_keys:
                db.clear_admin_login_rate_limits(conn, limiter_keys=account_keys)
            db.release_admin_login_admission(
                conn,
                limiter_key=source_key,
                now=now,
                rate_limit=settings.admin_login_rate_limit,
            )
    except Exception:
        _logger.warning(
            "Admin login rate limiter unavailable; cleared fallback only",
            exc_info=True,
        )


def clear_login_rate_limit(request: Request, settings: Settings, *, username: str = "") -> None:
    """Deprecated alias for :func:`finalize_successful_login`."""
    finalize_successful_login(request, settings, username=username)


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
