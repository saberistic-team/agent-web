"""Protected admin routes with login, logout, and session handling."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import admin_auth, admin_pages, db
from app.config import Settings, get_settings

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


def _issue_session(
    *,
    response: RedirectResponse,
    settings: Settings,
    admin_username: str,
    prior_raw_token: str | None,
) -> None:
    if prior_raw_token:
        prior_hash = admin_auth.hash_session_token(prior_raw_token)
        with db.db_connection(settings.database_url) as conn:
            db.revoke_admin_session(conn, token_hash=prior_hash)

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    expires_at = admin_auth.session_expires_at(settings)
    with db.db_connection(settings.database_url) as conn:
        db.create_admin_session(
            conn,
            token_hash=token_hash,
            admin_username=admin_username,
            expires_at=expires_at,
        )
    admin_auth.set_session_cookie(response, raw_token, settings)


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

    if admin_auth.is_login_throttled(request, settings):
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
        csrf = admin_auth.generate_csrf_token(settings)
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf,
                error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
                next_path=next,
            ),
            status_code=400,
        )

    if not admin_auth.verify_admin_credentials(username.strip(), password, settings):
        admin_auth.record_failed_login(request, settings)
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
    if raw_token is not None:
        token_hash = admin_auth.hash_session_token(raw_token)
        with db.db_connection(settings.database_url) as conn:
            db.revoke_admin_session(conn, token_hash=token_hash)
    response = RedirectResponse(url="/admin/login", status_code=303)
    admin_auth.clear_session_cookie(response, settings)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    return HTMLResponse(
        admin_pages.render_admin_dashboard_page(
            admin_username=session.admin_username,
            settings=settings,
        )
    )


@router.api_route("/{full_path:path}", methods=["GET", "HEAD"], response_model=None)
def admin_protected_fallback(request: Request, full_path: str) -> Response:
    """Redirect anonymous visitors for any other /admin path."""
    if full_path.rstrip("/") == "login":
        raise HTTPException(status_code=404, detail="Not found")
    require_admin_session(request)
    raise HTTPException(status_code=404, detail="Not found")
