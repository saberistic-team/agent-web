"""Single-operator admin authentication with server-side sessions."""

from __future__ import annotations

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
LOGIN_FLOW_CLEANUP_RETENTION_SECONDS = CSRF_MAX_AGE_SECONDS * 2
LOGIN_FLOW_CLEANUP_BATCH_SIZE = 100
INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."
INVALID_REQUEST_MESSAGE = "Invalid request."
LOGIN_THROTTLED_MESSAGE = "Too many login attempts. Try again later."

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
    """
    if settings.admin_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def build_rate_limit_key(username: str, client_source: str) -> str:
    """Derive a durable limiter key without storing raw username or IP.

    Key strategy: SHA-256 of ``normalized_username:client_source`` where the
    username is lowercased/stripped and the client source is the resolved IP
    from :func:`client_ip`.
    """
    normalized_username = username.strip().lower()
    material = f"{normalized_username}:{client_source}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_fallback_throttled(limiter_key: str) -> bool:
    now = time.time()
    with _fallback_lock:
        entry = _fallback_attempts.get(limiter_key)
        if entry is None:
            return False
        count, window_start = entry
        if now - window_start >= _FALLBACK_WINDOW_SECONDS:
            del _fallback_attempts[limiter_key]
            return False
        return count >= _FALLBACK_RATE_LIMIT


def _record_fallback_failure(limiter_key: str) -> None:
    now = time.time()
    with _fallback_lock:
        count, window_start = _fallback_attempts.get(limiter_key, (0, now))
        if now - window_start >= _FALLBACK_WINDOW_SECONDS:
            count = 0
            window_start = now
        _fallback_attempts[limiter_key] = (count + 1, window_start)


def _clear_fallback_failure(limiter_key: str) -> None:
    with _fallback_lock:
        _fallback_attempts.pop(limiter_key, None)


def is_login_throttled(request: Request, settings: Settings, *, username: str = "") -> bool:
    limiter_key = build_rate_limit_key(username, client_ip(request, settings))
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            return db.is_admin_login_throttled(conn, limiter_key=limiter_key, now=now)
    except Exception:
        _logger.warning(
            "Admin login rate limiter unavailable; using conservative fallback",
            exc_info=True,
        )
        return _is_fallback_throttled(limiter_key)


def record_failed_login(request: Request, settings: Settings, *, username: str = "") -> None:
    limiter_key = build_rate_limit_key(username, client_ip(request, settings))
    now = datetime.now(timezone.utc)
    try:
        with db.db_connection(settings.database_url) as conn:
            db.record_admin_login_failure(
                conn,
                limiter_key=limiter_key,
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
            "Admin login rate limiter unavailable; recording fallback failure",
            exc_info=True,
        )
        _record_fallback_failure(limiter_key)


def clear_login_rate_limit(request: Request, settings: Settings, *, username: str = "") -> None:
    limiter_key = build_rate_limit_key(username, client_ip(request, settings))
    _clear_fallback_failure(limiter_key)
    try:
        with db.db_connection(settings.database_url) as conn:
            db.clear_admin_login_rate_limit(conn, limiter_key=limiter_key)
    except Exception:
        _logger.warning(
            "Admin login rate limiter unavailable; cleared fallback only",
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
