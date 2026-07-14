"""Single-operator admin authentication with server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from fastapi import Request
from fastapi.responses import Response

from app.config import Settings

SESSION_COOKIE_NAME = "admin_session"
LOGIN_FLOW_COOKIE_NAME = "admin_login_flow"
CSRF_FORM_FIELD = "csrf_token"
CSRF_MAX_AGE_SECONDS = 900
INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."
INVALID_REQUEST_MESSAGE = "Invalid request."
LOGIN_THROTTLED_MESSAGE = "Too many login attempts. Try again later."

_password_hasher = PasswordHasher()
_rate_lock = Lock()
_login_attempts: dict[str, list[float]] = defaultdict(list)


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


def reset_login_rate_limiter() -> None:
    """Clear in-memory login attempt counters (tests only)."""
    with _rate_lock:
        _login_attempts.clear()


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


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def is_login_throttled(request: Request, settings: Settings) -> bool:
    ip = client_ip(request)
    now = time.time()
    window_start = now - settings.admin_login_rate_window_seconds
    with _rate_lock:
        attempts = [ts for ts in _login_attempts[ip] if ts >= window_start]
        _login_attempts[ip] = attempts
        return len(attempts) >= settings.admin_login_rate_limit


def record_failed_login(request: Request, settings: Settings) -> None:
    ip = client_ip(request)
    now = time.time()
    window_start = now - settings.admin_login_rate_window_seconds
    with _rate_lock:
        attempts = [ts for ts in _login_attempts[ip] if ts >= window_start]
        attempts.append(now)
        _login_attempts[ip] = attempts


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
