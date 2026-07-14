"""Protected admin routes with login, logout, and session handling."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app import admin, admin_auth, admin_pages, admin_research_pages, audit_service, brief_service, db
from app.actor_context import actor_context_from_request, anonymous_actor_context
from app.admin_layout import ADMIN_NAV_LINKS, render_admin_shell
from app.config import Settings, get_settings
from app.crm_service import CrmService
from app.research_records import ResearchRecordCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
_crm = CrmService()

PREVIEW_SESSION_TOKEN = "preview-screenshot-session"


def _verify_session_csrf(session: admin_auth.AdminSession, csrf_token: str) -> None:
    if not admin_auth.verify_csrf_value(csrf_token, session.csrf_token_hash):
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)


def _parse_research_form(
    *,
    record_type: str,
    body: str,
    contact_id: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    observed_value: str | None = None,
    observed_at: str | None = None,
    confidence: str | None = None,
    review_at: str | None = None,
    expires_at: str | None = None,
) -> ResearchRecordCreate:
    parsed_confidence: float | None = None
    if confidence is not None and confidence.strip():
        parsed_confidence = float(confidence)
    return ResearchRecordCreate(
        record_type=record_type,
        body=body,
        contact_id=contact_id,
        source_name=source_name,
        source_url=source_url,
        observed_value=observed_value,
        observed_at=observed_at,
        confidence=parsed_confidence,
        review_at=review_at,
        expires_at=expires_at,
    )


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


@router.get("/companies", response_class=HTMLResponse)
def admin_companies(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    if settings.admin_preview_enabled:
        from app.admin_preview import render_preview_section_main

        link = next(item for item in ADMIN_NAV_LINKS if item["href"] == "/admin/companies")
        return HTMLResponse(
            render_admin_shell(
                title=link["label"],
                main=render_preview_section_main(
                    label=link["label"],
                    summary=link["summary"],
                    active_path="/admin/companies",
                ),
                active_path="/admin/companies",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            )
        )
    with db.db_connection(settings.database_url) as conn:
        companies = _crm.list_companies(conn)
    return HTMLResponse(
        admin_research_pages.render_admin_companies_page(
            companies=companies,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def admin_company_research(
    request: Request,
    company_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        company = _crm.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        contacts = _crm.list_contacts_for_company(conn, company_id)
        records = _crm.list_research_for_company(conn, company_id)
    return HTMLResponse(
        admin_research_pages.render_admin_company_research_page(
            company=company,
            contacts=contacts,
            records=records,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            error_message=error,
        )
    )


@router.post("/companies/{company_id}/research", response_model=None)
def admin_company_research_create(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(..., alias="csrf_token"),
    record_type: str = Form(...),
    body: str = Form(...),
    contact_id: str | None = Form(default=None),
    source_name: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    observed_value: str | None = Form(default=None),
    observed_at: str | None = Form(default=None),
    confidence: str | None = Form(default=None),
    review_at: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
) -> Response:
    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(session, csrf_token)
    try:
        payload = _parse_research_form(
            record_type=record_type,
            body=body,
            contact_id=contact_id,
            source_name=source_name,
            source_url=source_url,
            observed_value=observed_value,
            observed_at=observed_at,
            confidence=confidence,
            review_at=review_at,
            expires_at=expires_at,
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/companies/{company_id}?error={quote(str(exc))}",
            status_code=303,
        )
    contact_uuid: UUID | None = None
    if payload.contact_id:
        contact_uuid = UUID(payload.contact_id)
    with db.db_connection(settings.database_url) as conn:
        company = _crm.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        if contact_uuid is not None:
            contact = _crm.get_contact(conn, contact_uuid)
            if contact is None or str(contact.get("company_id")) != str(company_id):
                return RedirectResponse(
                    url=f"/admin/companies/{company_id}?error=Invalid%20contact",
                    status_code=303,
                )
        _crm.attach_research_record(
            conn,
            record_type=payload.record_type,
            company_id=company_id,
            body=payload.body,
            contact_id=contact_uuid,
            source_name=payload.source_name,
            source_url=payload.source_url,
            observed_value=payload.observed_value,
            observed_at=payload.parsed_observed_at(),
            confidence=payload.confidence,
            review_at=payload.parsed_review_at(),
            expires_at=payload.parsed_expires_at(),
        )
    return RedirectResponse(url=f"/admin/companies/{company_id}", status_code=303)


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contact_research(
    request: Request,
    contact_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = ""
    if session.id:
        csrf_token = _issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        contact = _crm.get_contact(conn, contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        company = None
        if contact.get("company_id") is not None:
            company = _crm.get_company(conn, UUID(str(contact["company_id"])))
        records = _crm.list_research_for_contact(conn, contact_id)
    return HTMLResponse(
        admin_research_pages.render_admin_contact_research_page(
            contact=contact,
            company=company,
            records=records,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            error_message=error,
        )
    )


@router.post("/contacts/{contact_id}/research", response_model=None)
def admin_contact_research_create(
    request: Request,
    contact_id: UUID,
    csrf_token: str = Form(..., alias="csrf_token"),
    record_type: str = Form(...),
    body: str = Form(...),
    source_name: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    observed_value: str | None = Form(default=None),
    observed_at: str | None = Form(default=None),
    confidence: str | None = Form(default=None),
    review_at: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
) -> Response:
    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(session, csrf_token)
    try:
        payload = _parse_research_form(
            record_type=record_type,
            body=body,
            source_name=source_name,
            source_url=source_url,
            observed_value=observed_value,
            observed_at=observed_at,
            confidence=confidence,
            review_at=review_at,
            expires_at=expires_at,
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/contacts/{contact_id}?error={quote(str(exc))}",
            status_code=303,
        )
    with db.db_connection(settings.database_url) as conn:
        contact = _crm.get_contact(conn, contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        company_id = contact.get("company_id")
        if company_id is None:
            return RedirectResponse(
                url=f"/admin/contacts/{contact_id}?error=Contact%20has%20no%20company",
                status_code=303,
            )
        _crm.attach_research_record(
            conn,
            record_type=payload.record_type,
            company_id=UUID(str(company_id)),
            body=payload.body,
            contact_id=contact_id,
            source_name=payload.source_name,
            source_url=payload.source_url,
            observed_value=payload.observed_value,
            observed_at=payload.parsed_observed_at(),
            confidence=payload.confidence,
            review_at=payload.parsed_review_at(),
            expires_at=payload.parsed_expires_at(),
        )
    return RedirectResponse(url=f"/admin/contacts/{contact_id}", status_code=303)


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
    if settings.admin_preview_enabled:
        briefs, total, filters = brief_service.preview_briefs_list(
            page=page,
            per_page=settings.brief_page_size,
            query=q,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
    elif not settings.database_url:
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
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_audit_events

        all_events = build_preview_audit_events()
        total = len(all_events)
        safe_page = max(page, 1)
        per_page = settings.audit_page_size
        start = (safe_page - 1) * per_page
        events = all_events[start : start + per_page]
        page = safe_page
    elif not settings.database_url:
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


for _link in ADMIN_NAV_LINKS:
    if _link["href"] in {"/admin", "/admin/audit", "/admin/briefs", "/admin/companies"}:
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
