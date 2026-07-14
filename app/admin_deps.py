"""Shared dependencies for protected admin route handlers."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request

from app import admin_auth, db
from app.config import Settings, get_settings

PREVIEW_SESSION_TOKEN = "preview-screenshot-session"


def require_admin_auth_configured(settings: Settings) -> None:
    if not settings.admin_auth_configured:
        raise HTTPException(status_code=503, detail="Admin authentication not configured")


def _preview_session(settings: Settings) -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=0,
        admin_username=settings.admin_username or "preview",
        token_hash="preview",
        expires_at=datetime.max.replace(tzinfo=timezone.utc),
        csrf_token_hash=None,
    )


def load_valid_session(request: Request, settings: Settings) -> admin_auth.AdminSession | None:
    raw_token = admin_auth.read_session_token(request)
    if raw_token is None:
        return None
    if settings.admin_preview_mode and raw_token == PREVIEW_SESSION_TOKEN:
        return _preview_session(settings)
    token_hash = admin_auth.hash_session_token(raw_token)
    with db.db_connection(settings.database_url) as conn:
        row = db.get_admin_session_by_token_hash(conn, token_hash)
    if row is None or row.get("revoked_at") is not None:
        return None
    session = admin_auth.session_from_row(row)
    if session.expires_at <= datetime.now(timezone.utc):
        return None
    return session


def require_admin_session(request: Request) -> admin_auth.AdminSession:
    settings = get_settings()
    if settings.admin_preview_enabled:
        return admin_auth.preview_admin_session(settings)
    require_admin_auth_configured(settings)
    session = load_valid_session(request, settings)
    if session is None:
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        raise admin_auth.AdminLoginRequired(next_path)
    return session


def issue_session_csrf(settings: Settings, session_id: int) -> str:
    """Rotate the synchronizer token for an authenticated session."""
    raw_csrf_token = admin_auth.generate_csrf_value()
    csrf_hash = admin_auth.hash_csrf_token(raw_csrf_token)
    with db.db_connection(settings.database_url) as conn:
        db.update_admin_session_csrf(
            conn,
            session_id=session_id,
            csrf_token_hash=csrf_hash,
        )
    return raw_csrf_token


def verify_session_csrf(csrf_token: str, session: admin_auth.AdminSession) -> None:
    if not admin_auth.verify_csrf_value(csrf_token, session.csrf_token_hash):
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)
