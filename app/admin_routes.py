"""Protected admin routes with login, logout, and session handling."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app import admin, admin_auth, admin_companies as company_pages, admin_contacts as contact_pages, admin_dashboard_pages, admin_pages, admin_research_pages, audit_service, brief_service, db
from app.acquisition_dashboard import AcquisitionDashboardData, load_acquisition_dashboard
from app.companies import (
    COMPANY_CATEGORIES,
    COMPANY_STAGES,
    FRESHNESS_FILTERS,
    TARGET_STATUSES,
    CompanyCreate,
    CompanyUpdate,
)
from app.brief_conversion import (
    BriefConversionValidationError,
    pipeline_capabilities_available,
)
from app.contacts import BUYING_ROLES, ContactCreate, ContactUpdate
from app.crm_uow import crm_transaction
from app.actor_context import actor_context_from_request, anonymous_actor_context, correlation_id_from_request
from app.admin_layout import ADMIN_NAV_LINKS, render_admin_shell
from app.admin_preview import PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR, PREVIEW_BRIEF_DATABASE_ERROR_ID
from app.config import Settings, get_settings
from app.crm_service import CrmService
from app.research_records import ResearchRecordCreate
from app.repositories.postgres import get_repositories

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
_crm = CrmService()

PREVIEW_SESSION_TOKEN = "preview-screenshot-session"


def _verify_session_csrf(
    request: Request,
    session: admin_auth.AdminSession,
    csrf_token: str,
) -> None:
    settings = get_settings()
    if not admin_auth.verify_session_csrf_request(request, csrf_token, settings):
        raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)


def _session_csrf_for_forms(request: Request, settings: Settings) -> str:
    """Return the stable session-bound CSRF token for authenticated HTML forms."""
    if settings.admin_preview_enabled:
        return ""
    return admin_auth.session_csrf_for_request(request, settings)


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


def _company_form_payload(**values: object) -> dict[str, object]:
    """Map blank optional form fields to null before domain-model validation."""
    allowed = {
        "name",
        "website",
        "domain",
        "category",
        "stage",
        "headcount_estimate",
        "funding_summary",
        "target_status",
        "last_verified_at",
        "notes",
    }
    payload: dict[str, object] = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in values.items()
        if key in allowed
    }
    for field in (
        "website",
        "domain",
        "category",
        "stage",
        "funding_summary",
        "target_status",
        "last_verified_at",
        "notes",
    ):
        if not payload.get(field):
            payload[field] = None
    raw_headcount = payload.get("headcount_estimate")
    if raw_headcount in (None, ""):
        payload["headcount_estimate"] = None
    else:
        payload["headcount_estimate"] = int(str(raw_headcount))
    return payload


def _contact_form_payload(**values: object) -> dict[str, object]:
    """Map blank optional contact form fields to null before validation."""
    allowed = {
        "full_name",
        "title",
        "profile_url",
        "email",
        "email_permission",
        "company_id",
        "last_interaction_at",
        "relationship_strength",
        "notes",
        "buying_roles",
    }
    payload: dict[str, object] = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in values.items()
        if key in allowed
    }
    for field in (
        "title",
        "profile_url",
        "email",
        "email_permission",
        "last_interaction_at",
        "relationship_strength",
        "notes",
    ):
        if not payload.get(field):
            payload[field] = None
    raw_company = payload.get("company_id")
    if raw_company in (None, ""):
        payload["company_id"] = None
    else:
        payload["company_id"] = UUID(str(raw_company))
    roles = payload.get("buying_roles")
    if roles is None:
        payload["buying_roles"] = []
    elif isinstance(roles, str):
        payload["buying_roles"] = [roles]
    return payload


def _parse_link_choice(raw: str | None) -> tuple[str, UUID | None]:
    """Parse ``new`` or ``existing:{uuid}`` form values."""
    if not raw or raw.strip() == "new":
        return "new", None
    text = raw.strip()
    if text.startswith("existing:"):
        try:
            return "existing", UUID(text.split(":", 1)[1])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid selection.") from exc
    if text == "existing":
        return "existing", None
    raise HTTPException(status_code=400, detail="Invalid selection.")


def _brief_detail_context(
    conn,
    *,
    brief: dict,
    settings: Settings,
) -> tuple[bool, dict | None]:
    if not pipeline_capabilities_available(settings):
        return False, None
    source = _crm.get_project_brief_source(conn, int(brief["id"]))
    if source is None:
        return True, None
    result = _crm.get_brief_conversion_state(conn, int(brief["id"]))
    return True, result


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
            with crm_transaction(conn):
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
        try:
            db.cleanup_stale_admin_login_flows(
                conn,
                now=datetime.now(timezone.utc),
                expired_retention_seconds=admin_auth.LOGIN_FLOW_EXPIRED_RETENTION_SECONDS,
                consumed_retention_seconds=admin_auth.LOGIN_FLOW_CONSUMED_RETENTION_SECONDS,
                batch_size=admin_auth.LOGIN_FLOW_CLEANUP_BATCH_SIZE,
            )
        except Exception:
            logger.warning(
                "Admin login flow cleanup failed; continuing with new flow",
                exc_info=True,
            )
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


def _issue_session(
    *,
    request: Request,
    response: RedirectResponse,
    settings: Settings,
    admin_username: str,
    prior_raw_token: str | None,
) -> int:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    expires_at = admin_auth.session_expires_at(settings)
    derived_csrf = admin_auth.derive_session_csrf_token(raw_token, settings)
    csrf_hash = admin_auth.hash_csrf_token(derived_csrf)
    with db.db_connection(settings.database_url) as conn:
        with crm_transaction(conn):
            if prior_raw_token:
                prior_hash = admin_auth.hash_session_token(prior_raw_token)
                db.revoke_admin_session(conn, token_hash=prior_hash)
            session_id = db.create_admin_session(
                conn,
                token_hash=token_hash,
                admin_username=admin_username,
                expires_at=expires_at,
                csrf_token_hash=csrf_hash,
            )
            audit_service.record_login_success(
                conn,
                actor_context=actor_context_from_request(request, actor=admin_username),
                session_id=session_id,
            )
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
    """Authenticate an admin operator.

    Login-flow cookie lifecycle (``admin_login_flow``):

    * **Invalid CSRF** — consume the submitted flow (single-use), render a fresh
      form with a new CSRF token, and retain the replacement flow cookie.
    * **Invalid credentials** — same as invalid CSRF: consumed flow is not
      replayable; the replacement flow binds the returned CSRF token.
    * **Rate limited** — consume the submitted flow and retain a replacement so
      the operator can retry after lockout without refreshing.
    * **Success** — clear the pre-auth flow cookie and issue the session cookie.
    """
    settings = get_settings()
    _require_admin_auth_configured(settings)
    normalized_username = username.strip()

    if admin_auth.is_login_throttled(request, settings, username=normalized_username):
        _record_login_failure(
            request, reason="rate_limited", attempted_username=normalized_username
        )
        _consume_login_flow(request, settings)
        return _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.LOGIN_THROTTLED_MESSAGE,
            next_path=next,
            status_code=429,
        )

    csrf_valid = _verify_login_flow_csrf(request, settings, csrf_token)
    _consume_login_flow(request, settings)

    if not csrf_valid:
        admin_auth.record_failed_login(request, settings, username=normalized_username)
        _record_login_failure(
            request, reason="invalid_csrf", attempted_username=normalized_username
        )
        return _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
            next_path=next,
            status_code=400,
        )

    if not admin_auth.verify_admin_credentials(normalized_username, password, settings):
        admin_auth.record_failed_login(request, settings, username=normalized_username)
        _record_login_failure(
            request, reason="invalid_credentials", attempted_username=normalized_username
        )
        return _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
            next_path=next,
            status_code=401,
        )

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
        if not admin_auth.verify_session_csrf_request(request, csrf_token, settings):
            raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)
        raw_token = admin_auth.read_session_token(request)
        if raw_token is not None:
            token_hash = admin_auth.hash_session_token(raw_token)
            with db.db_connection(settings.database_url) as conn:
                with crm_transaction(conn):
                    db.revoke_admin_session(conn, token_hash=token_hash)
                    audit_service.record_logout(
                        conn,
                        actor_context=actor_context_from_request(
                            request, actor=session.admin_username
                        ),
                        session_id=session.id,
                    )
    else:
        if settings.database_url:
            try:
                with db.db_connection(settings.database_url) as conn:
                    with crm_transaction(conn):
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
def admin_companies(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    stage: str | None = None,
    target_status: str | None = None,
    freshness: str | None = None,
    archived: bool = False,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
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
    filters = {
        "q": q,
        "category": category if category in COMPANY_CATEGORIES else None,
        "stage": stage if stage in COMPANY_STAGES else None,
        "target_status": target_status if target_status in TARGET_STATUSES else None,
        "freshness": freshness if freshness in FRESHNESS_FILTERS else None,
        "archived": "1" if archived else None,
    }
    with db.db_connection(settings.database_url) as conn:
        companies = _crm.list_companies(
            conn,
            query=filters["q"],
            category=filters["category"],
            stage=filters["stage"],
            target_status=filters["target_status"],
            freshness=filters["freshness"],
            include_archived=archived,
        )
    return HTMLResponse(
        company_pages.render_companies_list_page(
            companies=companies,
            filters=filters,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.get("/companies/new", response_class=HTMLResponse)
def admin_company_new(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    csrf_token = _session_csrf_for_forms(request, get_settings())
    return HTMLResponse(
        company_pages.render_company_form_page(
            csrf_token=csrf_token, admin_username=session.admin_username
        )
    )


@router.post("/companies", response_model=None)
def admin_company_create(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    website: str | None = Form(default=None),
    domain: str | None = Form(default=None),
    category: str | None = Form(default=None),
    stage: str | None = Form(default=None),
    headcount_estimate: str | None = Form(default=None),
    funding_summary: str | None = Form(default=None),
    target_status: str | None = Form(default=None),
    last_verified_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        company = CompanyCreate(**_company_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(url=f"/admin/companies/new?error={quote(str(exc))}", status_code=303)
    with db.db_connection(get_settings().database_url) as conn:
        result = _crm.create_company(conn, company=company)
    warnings = result["duplicate_warnings"]
    warning = f"{len(warnings)} possible domain duplicate(s)" if warnings else ""
    return RedirectResponse(
        url=f"/admin/companies/{result['company']['id']}/edit?warning={quote(warning)}",
        status_code=303,
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def admin_company_research(
    request: Request,
    company_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
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


@router.get("/companies/{company_id}/edit", response_class=HTMLResponse)
def admin_company_edit(
    request: Request, company_id: UUID, error: str | None = None, warning: str | None = None
) -> HTMLResponse:
    session = require_admin_session(request)
    csrf_token = _session_csrf_for_forms(request, get_settings())
    with db.db_connection(get_settings().database_url) as conn:
        company = _crm.get_company(conn, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return HTMLResponse(
        company_pages.render_company_form_page(
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            company=company,
            error_message=error or warning,
        )
    )


@router.post("/companies/{company_id}/edit", response_model=None)
def admin_company_update(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
    name: str = Form(...),
    website: str | None = Form(default=None),
    domain: str | None = Form(default=None),
    category: str | None = Form(default=None),
    stage: str | None = Form(default=None),
    headcount_estimate: str | None = Form(default=None),
    funding_summary: str | None = Form(default=None),
    target_status: str | None = Form(default=None),
    last_verified_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        company = CompanyUpdate(**_company_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/companies/{company_id}/edit?error={quote(str(exc))}", status_code=303
        )
    with db.db_connection(get_settings().database_url) as conn:
        result = _crm.update_company(conn, company_id, company=company)
    if result is None:
        raise HTTPException(status_code=404, detail="Company not found")
    warnings = result["duplicate_warnings"]
    suffix = f"?warning={quote(f'{len(warnings)} possible domain duplicate(s)')}" if warnings else ""
    return RedirectResponse(url=f"/admin/companies/{company_id}/edit{suffix}", status_code=303)


@router.post("/companies/{company_id}/archive", response_model=None)
def admin_company_archive(request: Request, company_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.archive_company(conn, company_id) is None:
            raise HTTPException(status_code=404, detail="Company not found")
    return RedirectResponse(url="/admin/companies", status_code=303)


@router.post("/companies/{company_id}/restore", response_model=None)
def admin_company_restore(request: Request, company_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.restore_company(conn, company_id) is None:
            raise HTTPException(status_code=404, detail="Company not found")
    return RedirectResponse(url=f"/admin/companies/{company_id}", status_code=303)


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
    _verify_session_csrf(request, session, csrf_token)
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


@router.get("/contacts", response_class=HTMLResponse)
def admin_contacts(
    request: Request,
    q: str | None = None,
    company_id: str | None = None,
    buying_role: str | None = None,
    archived: bool = False,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import render_preview_section_main

        link = next(item for item in ADMIN_NAV_LINKS if item["href"] == "/admin/contacts")
        return HTMLResponse(
            render_admin_shell(
                title=link["label"],
                main=render_preview_section_main(
                    label=link["label"],
                    summary=link["summary"],
                    active_path="/admin/contacts",
                ),
                active_path="/admin/contacts",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            )
        )
    parsed_company_id: UUID | None = None
    if company_id:
        try:
            parsed_company_id = UUID(company_id)
        except ValueError:
            parsed_company_id = None
    filters = {
        "q": q,
        "company_id": company_id if parsed_company_id else None,
        "buying_role": buying_role if buying_role in BUYING_ROLES else None,
        "archived": "1" if archived else None,
    }
    with db.db_connection(settings.database_url) as conn:
        contacts = _crm.list_contacts(
            conn,
            query=filters["q"],
            company_id=parsed_company_id,
            buying_role=filters["buying_role"],
            include_archived=archived,
        )
        companies = _crm.list_companies(conn, limit=500)
    return HTMLResponse(
        contact_pages.render_contacts_list_page(
            contacts=contacts,
            companies=companies,
            filters=filters,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.get("/contacts/new", response_class=HTMLResponse)
def admin_contact_new(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    csrf_token = _session_csrf_for_forms(request, get_settings())
    with db.db_connection(get_settings().database_url) as conn:
        companies = _crm.list_companies(conn, limit=500)
    return HTMLResponse(
        contact_pages.render_contact_form_page(
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            companies=companies,
        )
    )


@router.post("/contacts", response_model=None)
def admin_contact_create(
    request: Request,
    csrf_token: str = Form(...),
    full_name: str = Form(...),
    title: str | None = Form(default=None),
    profile_url: str | None = Form(default=None),
    email: str | None = Form(default=None),
    email_permission: str | None = Form(default=None),
    company_id: str | None = Form(default=None),
    last_interaction_at: str | None = Form(default=None),
    relationship_strength: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    buying_roles: list[str] = Form(default=[]),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        contact = ContactCreate(**_contact_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(url=f"/admin/contacts/new?error={quote(str(exc))}", status_code=303)
    with db.db_connection(get_settings().database_url) as conn:
        result = _crm.create_contact(conn, contact=contact)
    warnings = result["duplicate_warnings"]
    warning = f"{len(warnings)} possible duplicate(s)" if warnings else ""
    return RedirectResponse(
        url=f"/admin/contacts/{result['contact']['id']}/edit?warning={quote(warning)}",
        status_code=303,
    )


@router.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
def admin_contact_edit(
    request: Request, contact_id: UUID, error: str | None = None, warning: str | None = None
) -> HTMLResponse:
    session = require_admin_session(request)
    csrf_token = _session_csrf_for_forms(request, get_settings())
    with db.db_connection(get_settings().database_url) as conn:
        contact = _crm.get_contact(conn, contact_id)
        companies = _crm.list_companies(conn, limit=500)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return HTMLResponse(
        contact_pages.render_contact_form_page(
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            companies=companies,
            contact=contact,
            error_message=error or warning,
        )
    )


@router.post("/contacts/{contact_id}/edit", response_model=None)
def admin_contact_update(
    request: Request,
    contact_id: UUID,
    csrf_token: str = Form(...),
    full_name: str = Form(...),
    title: str | None = Form(default=None),
    profile_url: str | None = Form(default=None),
    email: str | None = Form(default=None),
    email_permission: str | None = Form(default=None),
    company_id: str | None = Form(default=None),
    last_interaction_at: str | None = Form(default=None),
    relationship_strength: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    buying_roles: list[str] = Form(default=[]),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        contact = ContactUpdate(**_contact_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/contacts/{contact_id}/edit?error={quote(str(exc))}", status_code=303
        )
    with db.db_connection(get_settings().database_url) as conn:
        result = _crm.update_contact(conn, contact_id, contact=contact)
    if result is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    warnings = result["duplicate_warnings"]
    suffix = f"?warning={quote(f'{len(warnings)} possible duplicate(s)')}" if warnings else ""
    return RedirectResponse(url=f"/admin/contacts/{contact_id}/edit{suffix}", status_code=303)


@router.post("/contacts/{contact_id}/archive", response_model=None)
def admin_contact_archive(request: Request, contact_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.archive_contact(conn, contact_id) is None:
            raise HTTPException(status_code=404, detail="Contact not found")
    return RedirectResponse(url="/admin/contacts", status_code=303)


@router.post("/contacts/{contact_id}/restore", response_model=None)
def admin_contact_restore(request: Request, contact_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.restore_contact(conn, contact_id) is None:
            raise HTTPException(status_code=404, detail="Contact not found")
    return RedirectResponse(url=f"/admin/contacts/{contact_id}/edit", status_code=303)


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contact_research(
    request: Request,
    contact_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
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
    _verify_session_csrf(request, session, csrf_token)
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
    csrf_token = _session_csrf_for_forms(request, settings)
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
    csrf_token = _session_csrf_for_forms(request, settings)
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
        except brief_service.BRIEF_DATABASE_ERRORS:
            correlation_id = correlation_id_from_request(request)
            logger.exception(
                "Failed to load admin briefs list (correlation_id=%s)",
                correlation_id,
            )
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
    brief_id: str,
    page: int = 1,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    back_filters = brief_service.normalize_list_back_params(
        page=page,
        q=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    parsed_brief_id = brief_service.parse_brief_id(brief_id)
    if parsed_brief_id is None:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    if settings.admin_preview_enabled:
        if parsed_brief_id == PREVIEW_BRIEF_DATABASE_ERROR_ID:
            correlation_id = correlation_id_from_request(request)
            retry_href = request.url.path
            if request.url.query:
                retry_href = f"{retry_href}?{request.url.query}"
            return HTMLResponse(
                admin_pages.render_admin_brief_database_unavailable(
                    admin_username=session.admin_username,
                    back_filters=back_filters,
                    retry_href=retry_href,
                    correlation_id=correlation_id,
                    csrf_token=csrf_token,
                ),
                status_code=503,
            )
        brief = brief_service.preview_brief_detail(parsed_brief_id)
        if brief is None:
            return HTMLResponse(
                admin_pages.render_admin_brief_not_found(
                    brief_id=parsed_brief_id,
                    admin_username=session.admin_username,
                    back_filters=back_filters,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        from app.admin_preview import preview_brief_conversion_state, preview_pipeline_available

        pipeline_available = preview_pipeline_available()
        conversion = preview_brief_conversion_state(parsed_brief_id)
        converted = request.query_params.get("converted") == "1"
        return HTMLResponse(
            admin_pages.render_admin_brief_detail_page(
                admin_username=session.admin_username,
                brief=brief,
                back_filters=back_filters,
                price_cents=settings.brief_price_cents,
                csrf_token=csrf_token,
                pipeline_available=pipeline_available and conversion is None,
                conversion=conversion,
                converted=converted,
            )
        )
    if not settings.database_url:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=parsed_brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    try:
        with db.db_connection(settings.database_url) as conn:
            brief = brief_service.get_brief(conn, parsed_brief_id)
    except brief_service.BRIEF_DATABASE_ERRORS:
        correlation_id = correlation_id_from_request(request)
        logger.exception(
            "Failed to load admin brief detail for id %s (correlation_id=%s)",
            parsed_brief_id,
            correlation_id,
        )
        retry_href = request.url.path
        if request.url.query:
            retry_href = f"{retry_href}?{request.url.query}"
        return HTMLResponse(
            admin_pages.render_admin_brief_database_unavailable(
                admin_username=session.admin_username,
                back_filters=back_filters,
                retry_href=retry_href,
                correlation_id=correlation_id,
                csrf_token=csrf_token,
            ),
            status_code=503,
        )
    if brief is None:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=parsed_brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    converted = request.query_params.get("converted") == "1"
    with db.db_connection(settings.database_url) as conn:
        pipeline_available, conversion = _brief_detail_context(
            conn,
            brief=brief,
            settings=settings,
        )
    return HTMLResponse(
        admin_pages.render_admin_brief_detail_page(
            admin_username=session.admin_username,
            brief=brief,
            back_filters=back_filters,
            price_cents=settings.brief_price_cents,
            csrf_token=csrf_token,
            pipeline_available=pipeline_available and conversion is None,
            conversion=conversion,
            converted=converted,
        )
    )


@router.get("/briefs/{brief_id}/convert", response_class=HTMLResponse)
def admin_brief_convert_preview(
    request: Request,
    brief_id: str,
    page: int = 1,
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _issue_session_csrf(settings, session.id) if session.id else ""
    back_filters = brief_service.normalize_list_back_params(
        page=page,
        q=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    parsed_brief_id = brief_service.parse_brief_id(brief_id)
    if parsed_brief_id is None:
        return HTMLResponse(
            admin_pages.render_admin_brief_not_found(
                brief_id=brief_id,
                admin_username=session.admin_username,
                back_filters=back_filters,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    if not pipeline_capabilities_available(settings) and not settings.admin_preview_enabled:
        raise HTTPException(status_code=503, detail="Pipeline conversion is unavailable.")

    if settings.admin_preview_enabled:
        from app.admin_preview import preview_brief_convert_matches

        brief = brief_service.preview_brief_detail(parsed_brief_id)
        if brief is None:
            return HTMLResponse(
                admin_pages.render_admin_brief_not_found(
                    brief_id=parsed_brief_id,
                    admin_username=session.admin_username,
                    back_filters=back_filters,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        preview = preview_brief_convert_matches(
            parsed_brief_id,
            price_cents=settings.brief_price_cents,
        )
        error_message = request.query_params.get("error")
        if error_message == "validation":
            error_message = PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR
        return HTMLResponse(
            admin_pages.render_admin_brief_convert_page(
                admin_username=session.admin_username,
                brief=brief,
                back_filters=back_filters,
                preview=preview,
                csrf_token=csrf_token,
                error_message=error_message,
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Pipeline conversion is unavailable.")

    with db.db_connection(settings.database_url) as conn:
        brief = brief_service.get_brief(conn, parsed_brief_id)
        if brief is None:
            return HTMLResponse(
                admin_pages.render_admin_brief_not_found(
                    brief_id=parsed_brief_id,
                    admin_username=session.admin_username,
                    back_filters=back_filters,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        if _crm.get_project_brief_source(conn, parsed_brief_id) is not None:
            return RedirectResponse(
                url=f"/admin/briefs/{parsed_brief_id}?converted=1",
                status_code=303,
            )
        preview = _crm.find_brief_conversion_matches(
            conn,
            brief,
            price_cents=settings.brief_price_cents,
        )
    return HTMLResponse(
        admin_pages.render_admin_brief_convert_page(
            admin_username=session.admin_username,
            brief=brief,
            back_filters=back_filters,
            preview=preview,
            csrf_token=csrf_token,
            error_message=request.query_params.get("error"),
        )
    )


@router.post("/briefs/{brief_id}/convert")
def admin_brief_convert_confirm(
    request: Request,
    brief_id: str,
    csrf_token: str = Form(...),
    company_choice: str = Form(default="new"),
    contact_choice: str = Form(default="new"),
    page: int = 1,
    q: str | None = Form(default=None),
    status: str | None = Form(default=None),
    date_from: str | None = Form(default=None),
    date_to: str | None = Form(default=None),
) -> RedirectResponse:
    session = require_admin_session(request)
    _verify_session_csrf(session, csrf_token)
    settings = get_settings()
    parsed_brief_id = brief_service.parse_brief_id(brief_id)
    if parsed_brief_id is None:
        raise HTTPException(status_code=404, detail="Brief not found.")
    if not pipeline_capabilities_available(settings) and not settings.admin_preview_enabled:
        raise HTTPException(status_code=503, detail="Pipeline conversion is unavailable.")

    company_mode, selected_company_id = _parse_link_choice(company_choice)
    contact_mode, selected_contact_id = _parse_link_choice(contact_choice)
    detail_url = f"/admin/briefs/{parsed_brief_id}?converted=1"

    if settings.admin_preview_enabled:
        from app.admin_preview import preview_brief_convert_post

        error = preview_brief_convert_post(
            parsed_brief_id,
            company_mode=company_mode,
            contact_mode=contact_mode,
            selected_company_id=selected_company_id,
            selected_contact_id=selected_contact_id,
        )
        if error:
            return RedirectResponse(
                url=f"/admin/briefs/{parsed_brief_id}/convert?error={quote(error)}",
                status_code=303,
            )
        return RedirectResponse(url=detail_url, status_code=303)

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Pipeline conversion is unavailable.")

    actor_context = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            brief = brief_service.get_brief(conn, parsed_brief_id)
            if brief is None:
                raise HTTPException(status_code=404, detail="Brief not found.")
            _crm.convert_project_brief(
                conn,
                brief=brief,
                actor_context=actor_context,
                price_cents=settings.brief_price_cents,
                company_choice=company_mode,
                contact_choice=contact_mode,
                selected_company_id=selected_company_id,
                selected_contact_id=selected_contact_id,
            )
    except BriefConversionValidationError as exc:
        return RedirectResponse(
            url=f"/admin/briefs/{parsed_brief_id}/convert?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url=detail_url, status_code=303)


@router.get("/audit", response_class=HTMLResponse)
def admin_audit_list(request: Request, page: int = 1) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
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
    if _link["href"] in {"/admin", "/admin/audit", "/admin/briefs", "/admin/companies", "/admin/contacts", "/admin/pipeline"}:
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
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_acquisition_dashboard_data

        return HTMLResponse(
            admin_dashboard_pages.render_acquisition_dashboard_page(
                data=build_preview_acquisition_dashboard_data(),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )
    dashboard_data: AcquisitionDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_acquisition_dashboard(
                    conn,
                    get_repositories().acquisition_dashboard,
                )
        except Exception:
            logger.exception("Failed to load acquisition dashboard")
            db_error = True
    if dashboard_data is None:
        from datetime import datetime as dt

        dashboard_data = AcquisitionDashboardData(
            company_counts_by_stage=(),
            company_counts_by_category=(),
            contact_counts_by_stage=(),
            contact_counts_by_category=(),
            overdue_actions=(),
            upcoming_actions=(),
            recent_evidence=(),
            stale_evidence=(),
            without_decision_maker=(),
            without_next_action=(),
            generated_at=dt.now(timezone.utc),
        )
    return HTMLResponse(
        admin_dashboard_pages.render_acquisition_dashboard_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.api_route("/{full_path:path}", methods=["GET", "HEAD"], response_model=None)
def admin_protected_fallback(request: Request, full_path: str) -> Response:
    """Auth gate + shell 404 for unknown /admin paths."""
    if full_path.rstrip("/") == "login":
        raise HTTPException(status_code=404, detail="Not found")
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    path = f"/admin/{full_path.rstrip('/')}"
    return HTMLResponse(
        admin.render_admin_not_found(
            path,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        ),
        status_code=404,
    )
