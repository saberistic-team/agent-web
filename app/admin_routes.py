"""Protected admin routes with login, logout, audit trail, and session handling."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import admin_auth, admin_pages, audit_service, db
from app.actor_context import actor_context_from_request, anonymous_actor_context
from app.admin_layout import ADMIN_NAV_LINKS
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin_auth_configured(settings: Settings) -> None:
    if not settings.admin_auth_configured:
        raise HTTPException(status_code=503, detail="Admin authentication not configured")


def _load_valid_session(request: Request, settings: Settings) -> admin_auth.AdminSession | None:
    raw_token = admin_auth.read_session_token(request)
    if raw_token is None:
        return None
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
    _require_admin_auth_configured(settings)
    session = _load_valid_session(request, settings)
    if session is None:
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        raise admin_auth.AdminLoginRequired(next_path)
    return session


def _record_login_failure(
    request: Request,
    *,
    reason: str,
    attempted_username: str | None = None,
) -> None:
    settings = get_settings()
    actor = attempted_username.strip() if attempted_username else "anonymous"
    actor_context = actor_context_from_request(request, actor=actor)
    try:
        with db.db_connection(settings.database_url) as conn:
            audit_service.record_login_failure(
                conn,
                actor_context=actor_context,
                reason=reason,
                attempted_username=attempted_username,
            )
    except Exception:
        logger.exception("Failed to record login failure audit event")


def _issue_session(
    *,
    request: Request,
    response: RedirectResponse,
    settings: Settings,
    admin_username: str,
    prior_raw_token: str | None,
) -> int:
    if prior_raw_token:
        prior_hash = admin_auth.hash_session_token(prior_raw_token)
        with db.db_connection(settings.database_url) as conn:
            db.revoke_admin_session(conn, token_hash=prior_hash)

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    expires_at = admin_auth.session_expires_at(settings)
    with db.db_connection(settings.database_url) as conn:
        session_id = db.create_admin_session(
            conn,
            token_hash=token_hash,
            admin_username=admin_username,
            expires_at=expires_at,
        )
        try:
            audit_service.record_login_success(
                conn,
                actor_context=actor_context_from_request(request, actor=admin_username),
                session_id=session_id,
            )
        except Exception:
            logger.exception("Failed to record login success audit event")
    admin_auth.set_session_cookie(response, raw_token, settings)
    return session_id


@router.get("/login", response_class=HTMLResponse, response_model=None)
def admin_login_form(request: Request, next: str | None = None) -> Response:
    settings = get_settings()
    _require_admin_auth_configured(settings)
    if _load_valid_session(request, settings) is not None:
        return RedirectResponse(
            url=admin_auth.safe_admin_next_path(next),
            status_code=303,
        )
    csrf_token = admin_auth.generate_csrf_token(settings)
    return HTMLResponse(
        admin_pages.render_admin_login_page(
            csrf_token=csrf_token,
            next_path=next,
        )
    )


@router.post("/login", response_model=None)
def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(..., alias="csrf_token"),
    next: str | None = Form(default=None),
) -> Response:
    settings = get_settings()
    _require_admin_auth_configured(settings)
    attempted_username = username.strip()

    if admin_auth.is_login_throttled(request, settings):
        _record_login_failure(request, reason="rate_limited", attempted_username=attempted_username)
        csrf = admin_auth.generate_csrf_token(settings)
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf,
                error_message=admin_auth.LOGIN_THROTTLED_MESSAGE,
                next_path=next,
            ),
            status_code=429,
        )

    if not admin_auth.verify_csrf_token(csrf_token, settings):
        admin_auth.record_failed_login(request, settings)
        _record_login_failure(request, reason="invalid_csrf", attempted_username=attempted_username)
        csrf = admin_auth.generate_csrf_token(settings)
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf,
                error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
                next_path=next,
            ),
            status_code=400,
        )

    if not admin_auth.verify_admin_credentials(attempted_username, password, settings):
        admin_auth.record_failed_login(request, settings)
        _record_login_failure(
            request,
            reason="invalid_credentials",
            attempted_username=attempted_username,
        )
        csrf = admin_auth.generate_csrf_token(settings)
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf,
                error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
                next_path=next,
            ),
            status_code=401,
        )

    destination = admin_auth.safe_admin_next_path(next)
    response = RedirectResponse(url=destination, status_code=303)
    _issue_session(
        request=request,
        response=response,
        settings=settings,
        admin_username=settings.admin_username,
        prior_raw_token=admin_auth.read_session_token(request),
    )
    return response


@router.post("/logout")
def admin_logout(request: Request) -> RedirectResponse:
    settings = get_settings()
    _require_admin_auth_configured(settings)
    raw_token = admin_auth.read_session_token(request)
    session_id: int | None = None
    actor = "anonymous"
    if raw_token is not None:
        token_hash = admin_auth.hash_session_token(raw_token)
        try:
            with db.db_connection(settings.database_url) as conn:
                row = db.get_admin_session_by_token_hash(conn, token_hash)
                if row is not None:
                    session_id = int(row["id"])
                    actor = str(row["admin_username"])
                db.revoke_admin_session(conn, token_hash=token_hash)
                try:
                    audit_service.record_logout(
                        conn,
                        actor_context=actor_context_from_request(request, actor=actor),
                        session_id=session_id,
                    )
                except Exception:
                    logger.exception("Failed to record logout audit event")
        except Exception:
            logger.exception("Failed to revoke admin session during logout")
    else:
        try:
            with db.db_connection(settings.database_url) as conn:
                audit_service.record_logout(
                    conn,
                    actor_context=anonymous_actor_context(request),
                    session_id=None,
                )
        except Exception:
            logger.exception("Failed to record anonymous logout audit event")
    response = RedirectResponse(url="/admin/login", status_code=303)
    admin_auth.clear_session_cookie(response, settings)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    return HTMLResponse(
        admin_pages.render_admin_dashboard_page(admin_username=session.admin_username)
    )


@router.get("/audit", response_class=HTMLResponse)
def admin_audit_list(request: Request, page: int = 1) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    with db.db_connection(settings.database_url) as conn:
        events, total = audit_service.list_events(
            conn,
            page=page,
            per_page=settings.audit_page_size,
        )
    return HTMLResponse(
        admin_pages.render_admin_audit_page(
            admin_username=session.admin_username,
            events=events,
            page=max(page, 1),
            per_page=settings.audit_page_size,
            total=total,
        )
    )


@router.api_route("/{full_path:path}", methods=["GET", "HEAD"], response_model=None)
def admin_protected_fallback(request: Request, full_path: str) -> Response:
    """Serve placeholder pages or redirect anonymous visitors."""
    if full_path.rstrip("/") == "login":
        raise HTTPException(status_code=404, detail="Not found")
    session = require_admin_session(request)
    path = f"/admin/{full_path}".rstrip("/") if full_path else "/admin"
    link = next((item for item in ADMIN_NAV_LINKS if item["href"] == path), None)
    if link is None:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(
        admin_pages.render_admin_placeholder_page(
            admin_username=session.admin_username,
            active_path=path,
            label=link["label"],
        )
    )
