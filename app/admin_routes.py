"""Protected admin routes with login, logout, and session handling."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import admin, admin_auth, admin_companies, admin_contacts, admin_pages, audit_service, brief_service, db
from app.actor_context import actor_context_from_request, anonymous_actor_context
from app.admin_layout import ADMIN_NAV_LINKS
from app.config import Settings, get_settings
from app.crm_service import CrmService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

PREVIEW_SESSION_TOKEN = "preview-screenshot-session"


def _crm_service() -> CrmService:
    return CrmService()


def _require_crm_db(settings: Settings) -> None:
    if not settings.database_configured:
        raise HTTPException(status_code=503, detail="Database not configured")


def _verify_session_csrf(csrf_token: str, session: admin_auth.AdminSession) -> None:
    if not admin_auth.verify_csrf_value(csrf_token, session.csrf_token_hash):
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)


def _require_admin_auth_configured(settings: Settings) -> None:
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


def _load_valid_session(request: Request, settings: Settings) -> admin_auth.AdminSession | None:
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
    if not settings.database_url:
        return
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


def _issue_login_flow_response(
    *,
    settings: Settings,
    next_path: str | None = None,
    error_message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Mint a browser-bound pre-auth flow and render the login form."""
    raw_flow_token = admin_auth.generate_session_token()
    raw_csrf_token = admin_auth.generate_csrf_value()
    flow_hash = admin_auth.hash_session_token(raw_flow_token)
    csrf_hash = admin_auth.hash_csrf_token(raw_csrf_token)
    expires_at = admin_auth.login_flow_expires_at()
    with db.db_connection(settings.database_url) as conn:
        db.create_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            expires_at=expires_at,
        )
    response = HTMLResponse(
        admin_pages.render_admin_login_page(
            csrf_token=raw_csrf_token,
            error_message=error_message,
            next_path=next_path,
        ),
        status_code=status_code,
    )
    admin_auth.set_login_flow_cookie(response, raw_flow_token, settings)
    return response


def _verify_login_flow_csrf(
    request: Request,
    settings: Settings,
    csrf_token: str,
) -> bool:
    """Validate a login CSRF token against the initiating browser flow."""
    raw_flow_token = admin_auth.read_login_flow_token(request)
    if raw_flow_token is None:
        return False
    flow_hash = admin_auth.hash_session_token(raw_flow_token)
    with db.db_connection(settings.database_url) as conn:
        row = db.get_admin_login_flow_by_token_hash(conn, flow_hash)
    if row is None or row.get("consumed_at") is not None:
        return False
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return False
    return admin_auth.verify_csrf_value(csrf_token, row.get("csrf_token_hash"))


def _consume_login_flow(request: Request, settings: Settings) -> None:
    raw_flow_token = admin_auth.read_login_flow_token(request)
    if raw_flow_token is None:
        return
    flow_hash = admin_auth.hash_session_token(raw_flow_token)
    with db.db_connection(settings.database_url) as conn:
        db.consume_admin_login_flow(conn, flow_token_hash=flow_hash)


def _issue_session_csrf(settings: Settings, session_id: int) -> str:
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
    initial_csrf = admin_auth.generate_csrf_value()
    csrf_hash = admin_auth.hash_csrf_token(initial_csrf)
    with db.db_connection(settings.database_url) as conn:
        session_id = db.create_admin_session(
            conn,
            token_hash=token_hash,
            admin_username=admin_username,
            expires_at=expires_at,
            csrf_token_hash=csrf_hash,
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
    if settings.admin_preview_enabled:
        # Preview mode: render login UI without requiring live auth secrets/DB.
        csrf_token = (
            admin_auth.generate_csrf_value()
            if settings.admin_session_secret
            else "preview-csrf"
        )
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf_token,
                next_path=next,
            )
        )
    _require_admin_auth_configured(settings)
    if _load_valid_session(request, settings) is not None:
        return RedirectResponse(
            url=admin_auth.safe_admin_next_path(next),
            status_code=303,
        )
    return _issue_login_flow_response(settings=settings, next_path=next)


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
    normalized_username = username.strip()

    if admin_auth.is_login_throttled(request, settings, username=normalized_username):
        _record_login_failure(
            request, reason="rate_limited", attempted_username=normalized_username
        )
        _consume_login_flow(request, settings)
        response = _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.LOGIN_THROTTLED_MESSAGE,
            next_path=next,
            status_code=429,
        )
        admin_auth.clear_login_flow_cookie(response, settings)
        return response

    csrf_valid = _verify_login_flow_csrf(request, settings, csrf_token)
    _consume_login_flow(request, settings)

    if not csrf_valid:
        admin_auth.record_failed_login(request, settings, username=normalized_username)
        _record_login_failure(
            request, reason="invalid_csrf", attempted_username=normalized_username
        )
        response = _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
            next_path=next,
            status_code=400,
        )
        admin_auth.clear_login_flow_cookie(response, settings)
        return response

    if not admin_auth.verify_admin_credentials(normalized_username, password, settings):
        admin_auth.record_failed_login(request, settings, username=normalized_username)
        _record_login_failure(
            request, reason="invalid_credentials", attempted_username=normalized_username
        )
        response = _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
            next_path=next,
            status_code=401,
        )
        admin_auth.clear_login_flow_cookie(response, settings)
        return response

    destination = admin_auth.safe_admin_next_path(next)
    response = RedirectResponse(url=destination, status_code=303)
    admin_auth.clear_login_rate_limit(request, settings, username=normalized_username)
    _issue_session(
        request=request,
        response=response,
        settings=settings,
        admin_username=settings.admin_username,
        prior_raw_token=admin_auth.read_session_token(request),
    )
    admin_auth.clear_login_flow_cookie(response, settings)
    return response


@router.post("/logout")
def admin_logout(
    request: Request,
    csrf_token: str = Form(..., alias="csrf_token"),
) -> Response:
    settings = get_settings()
    _require_admin_auth_configured(settings)
    session = _load_valid_session(request, settings)
    if session is not None:
        if not admin_auth.verify_csrf_value(csrf_token, session.csrf_token_hash):
            raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)
        raw_token = admin_auth.read_session_token(request)
        if raw_token is not None:
            token_hash = admin_auth.hash_session_token(raw_token)
            with db.db_connection(settings.database_url) as conn:
                db.revoke_admin_session(conn, token_hash=token_hash)
                try:
                    audit_service.record_logout(
                        conn,
                        actor_context=actor_context_from_request(
                            request, actor=session.admin_username
                        ),
                        session_id=session.id,
                    )
                except Exception:
                    logger.exception("Failed to record logout audit event")
    else:
        if settings.database_url:
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


def _render_admin_shell_page(request: Request, active_path: str) -> HTMLResponse:
    """Authenticate and render the shared admin shell for a nav path."""
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    kwargs = dict(admin_username=session.admin_username, csrf_token=csrf_token)
    if not admin.is_admin_path(active_path):
        return HTMLResponse(admin.render_admin_not_found(active_path, **kwargs), status_code=404)
    return HTMLResponse(admin.render_admin_page(active_path, **kwargs))


@router.get("/briefs", response_class=HTMLResponse)
def admin_briefs_list(
    request: Request,
    page: int = 1,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    briefs: list[dict] = []
    total = 0
    filters = brief_service.normalize_filters(
        page=page,
        per_page=settings.brief_page_size,
        query=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    db_error = False
    if settings.admin_preview_enabled or not settings.database_url:
        pass
    else:
        try:
            with db.db_connection(settings.database_url) as conn:
                briefs, total, filters = brief_service.list_briefs(
                    conn,
                    page=page,
                    per_page=settings.brief_page_size,
                    query=q,
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                )
        except Exception:
            logger.exception("Failed to load admin briefs list")
            db_error = True
    return HTMLResponse(
        admin_pages.render_admin_briefs_page(
            admin_username=session.admin_username,
            briefs=briefs,
            filters=filters,
            total=total,
            price_cents=settings.brief_price_cents,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/briefs/{brief_id}", response_class=HTMLResponse)
def admin_brief_detail(
    request: Request,
    brief_id: int,
    page: int = 1,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    back_filters = brief_service.normalize_list_back_params(
        page=page,
        q=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    if settings.admin_preview_enabled:
        brief = brief_service.preview_brief_detail(brief_id)
        if brief is None:
            return HTMLResponse(
                admin_pages.render_admin_brief_not_found(
                    brief_id=brief_id,
                    admin_username=session.admin_username,
                    back_filters=back_filters,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        return HTMLResponse(
            admin_pages.render_admin_brief_detail_page(
                admin_username=session.admin_username,
                brief=brief,
                back_filters=back_filters,
                price_cents=settings.brief_price_cents,
                csrf_token=csrf_token,
            )
        )
    if not settings.database_url:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    try:
        with db.db_connection(settings.database_url) as conn:
            brief = brief_service.get_brief(conn, brief_id)
    except Exception:
        logger.exception("Failed to load admin brief detail for id %s", brief_id)
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    if brief is None:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    return HTMLResponse(
        admin_pages.render_admin_brief_detail_page(
            admin_username=session.admin_username,
            brief=brief,
            back_filters=back_filters,
            price_cents=settings.brief_price_cents,
            csrf_token=csrf_token,
        )
    )


@router.get("/audit", response_class=HTMLResponse)
def admin_audit_list(request: Request, page: int = 1) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    if settings.admin_preview_enabled or not settings.database_url:
        events, total = [], 0
    else:
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
            csrf_token=csrf_token,
        )
    )


@router.get("/contacts", response_class=HTMLResponse)
def admin_contacts_list(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    query = request.query_params.get("q", "")
    include_archived = request.query_params.get("include_archived") == "1"
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        contacts = _crm_service().search_contacts(
            conn,
            query=query,
            include_archived=include_archived,
        )
    return HTMLResponse(
        admin_contacts.render_contacts_list_page(
            admin_username=session.admin_username,
            contacts=contacts,
            query=query,
            include_archived=include_archived,
            csrf_token=csrf_token,
        )
    )


@router.get("/contacts/new", response_class=HTMLResponse)
def admin_contacts_new_form(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    company_id = request.query_params.get("company_id")
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        companies = _crm_service()._repos.companies.list_all(conn)
    contact: dict[str, object] = {}
    if company_id:
        contact["company_id"] = company_id
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
            admin_username=session.admin_username,
            companies=companies,
            contact=contact,
            is_new=True,
            csrf_token=csrf_token,
        )
    )


@router.post("/contacts/new", response_class=HTMLResponse)
def admin_contacts_create(
    request: Request,
    name: str = Form(...),
    title: str = Form(""),
    profile_url: str = Form(""),
    company_id: str = Form(""),
    email: str = Form(""),
    email_permission: str = Form(""),
    email_provenance: str = Form(""),
    last_interaction_at: str = Form(""),
    relationship_strength: str = Form(""),
    notes: str = Form(""),
    buying_roles: list[str] = Form(default=[]),
    csrf_token: str = Form(..., alias="csrf_token"),
) -> HTMLResponse:
    session = require_admin_session(request)
    _verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = _issue_session_csrf(settings, session.id)
    if not name.strip():
        with db.db_connection(settings.database_url) as conn:
            companies = _crm_service()._repos.companies.list_all(conn)
        return HTMLResponse(
            admin_contacts.render_contact_form_page(
                admin_username=session.admin_username,
                companies=companies,
                is_new=True,
                error="Name is required.",
                csrf_token=csrf_token,
            ),
            status_code=400,
        )
    payload = admin_contacts.parse_contact_form(
        name=name,
        title=title,
        profile_url=profile_url,
        company_id=company_id,
        email=email,
        email_permission=email_permission,
        email_provenance=email_provenance,
        last_interaction_at=last_interaction_at,
        relationship_strength=relationship_strength,
        notes=notes,
        buying_roles=buying_roles,
    )
    with db.db_connection(settings.database_url) as conn:
        contact = _crm_service().create_contact(conn, **payload)
        companies = _crm_service()._repos.companies.list_all(conn)
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
            admin_username=session.admin_username,
            companies=companies,
            contact=contact,
            warnings=contact.get("duplicate_warnings"),
            csrf_token=csrf_token,
        )
    )


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contacts_edit_form(request: Request, contact_id: str) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        contact = _crm_service().get_contact_with_roles(conn, UUID(contact_id))
        companies = _crm_service()._repos.companies.list_all(conn)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
            admin_username=session.admin_username,
            companies=companies,
            contact=contact,
            csrf_token=csrf_token,
        )
    )


@router.post("/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contacts_update(
    request: Request,
    contact_id: str,
    name: str = Form(...),
    title: str = Form(""),
    profile_url: str = Form(""),
    company_id: str = Form(""),
    email: str = Form(""),
    email_permission: str = Form(""),
    email_provenance: str = Form(""),
    last_interaction_at: str = Form(""),
    relationship_strength: str = Form(""),
    notes: str = Form(""),
    buying_roles: list[str] = Form(default=[]),
    csrf_token: str = Form(..., alias="csrf_token"),
) -> HTMLResponse:
    session = require_admin_session(request)
    _verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = _issue_session_csrf(settings, session.id)
    if not name.strip():
        with db.db_connection(settings.database_url) as conn:
            companies = _crm_service()._repos.companies.list_all(conn)
            contact = _crm_service().get_contact_with_roles(conn, UUID(contact_id))
        return HTMLResponse(
            admin_contacts.render_contact_form_page(
                admin_username=session.admin_username,
                companies=companies,
                contact=contact,
                error="Name is required.",
                csrf_token=csrf_token,
            ),
            status_code=400,
        )
    payload = admin_contacts.parse_contact_form(
        name=name,
        title=title,
        profile_url=profile_url,
        company_id=company_id,
        email=email,
        email_permission=email_permission,
        email_provenance=email_provenance,
        last_interaction_at=last_interaction_at,
        relationship_strength=relationship_strength,
        notes=notes,
        buying_roles=buying_roles,
    )
    with db.db_connection(settings.database_url) as conn:
        contact = _crm_service().update_contact(conn, UUID(contact_id), **payload)
        companies = _crm_service()._repos.companies.list_all(conn)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
            admin_username=session.admin_username,
            companies=companies,
            contact=contact,
            warnings=contact.get("duplicate_warnings"),
            csrf_token=csrf_token,
        )
    )


@router.post("/contacts/{contact_id}/archive")
def admin_contacts_archive(
    request: Request,
    contact_id: str,
    csrf_token: str = Form(..., alias="csrf_token"),
) -> RedirectResponse:
    session = require_admin_session(request)
    _verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    with db.db_connection(settings.database_url) as conn:
        _crm_service().archive_contact(conn, UUID(contact_id))
    return RedirectResponse(url="/admin/contacts", status_code=303)


@router.post("/contacts/{contact_id}/restore")
def admin_contacts_restore(
    request: Request,
    contact_id: str,
    csrf_token: str = Form(..., alias="csrf_token"),
) -> RedirectResponse:
    session = require_admin_session(request)
    _verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    with db.db_connection(settings.database_url) as conn:
        _crm_service().restore_contact(conn, UUID(contact_id))
    return RedirectResponse(url=f"/admin/contacts/{contact_id}", status_code=303)


@router.get("/companies", response_class=HTMLResponse)
def admin_companies_list(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        companies = _crm_service()._repos.companies.list_all(conn)
    return HTMLResponse(
        admin_companies.render_companies_list_page(
            admin_username=session.admin_username,
            companies=companies,
            csrf_token=csrf_token,
        )
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def admin_company_detail(request: Request, company_id: str) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        company = _crm_service()._repos.companies.get_by_id(conn, UUID(company_id))
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        contacts = _crm_service().list_company_contacts(conn, UUID(company_id))
    return HTMLResponse(
        admin_contacts.render_company_detail_page(
            admin_username=session.admin_username,
            company=company,
            contacts=contacts,
            csrf_token=csrf_token,
        )
    )


for _link in ADMIN_NAV_LINKS:
    if _link["href"] in {
        "/admin",
        "/admin/audit",
        "/admin/briefs",
        "/admin/contacts",
        "/admin/companies",
    }:
        continue
    _section = _link["href"].removeprefix("/admin/")

    def _section_handler(
        request: Request,
        *,
        _active_path: str = _link["href"],
    ) -> HTMLResponse:
        return _render_admin_shell_page(request, _active_path)

    router.add_api_route(
        f"/{_section}",
        _section_handler,
        methods=["GET"],
        response_class=HTMLResponse,
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    return _render_admin_shell_page(request, "/admin")


@router.api_route("/{full_path:path}", methods=["GET", "HEAD"], response_model=None)
def admin_protected_fallback(request: Request, full_path: str) -> Response:
    """Auth gate + shell 404 for unknown /admin paths."""
    if full_path.rstrip("/") == "login":
        raise HTTPException(status_code=404, detail="Not found")
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    path = f"/admin/{full_path.rstrip('/')}"
    return HTMLResponse(
        admin.render_admin_not_found(
            path,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        ),
        status_code=404,
    )
