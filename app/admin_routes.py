"""Protected admin routes with login, logout, and session handling."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from app import admin, admin_auth, admin_companies as company_pages, admin_contacts as contact_pages, admin_dashboard_pages, admin_import_batches, admin_imports as import_pages, admin_pages, admin_research_pages, audit_service, brief_service, db
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
    effective_brief_price_cents,
    pipeline_capabilities_available,
)
from app.contacts import (
    BUYING_ROLES,
    ContactCreate,
    ContactEmailConflictError,
    ContactSafeSummary,
    ContactUpdate,
)
from app.crm_uow import crm_transaction
from app.actor_context import (
    actor_context_from_request,
    anonymous_actor_context,
    correlation_id_from_request,
)
from app.admin_layout import ADMIN_NAV_LINKS, render_admin_shell
from app.admin_response import admin_html_response
from app.admin_response_policy import csp_nonce_from_request
from app.admin_preview import (
    PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR,
    PREVIEW_BRIEF_DATABASE_ERROR_ID,
    PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
    PREVIEW_COMPANY_DETAIL_RESTORE_ID,
    PREVIEW_COMPANY_VALIDATION_ERROR,
    PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
    PREVIEW_CONTACT_DETAIL_RESTORE_ID,
    PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID,
    build_preview_companies_for_select,
    build_preview_company,
    build_preview_company_contacts,
    build_preview_company_detail,
    build_preview_company_research,
    build_preview_contact,
    build_preview_contact_detail,
    build_preview_contact_research,
    preview_contact_restore_conflict,
)
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
        "crm_context_tags",
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
    tags = payload.get("crm_context_tags")
    if tags is None:
        payload["crm_context_tags"] = []
    elif isinstance(tags, str):
        payload["crm_context_tags"] = [tags]
    return payload


def _parse_link_choice(raw: str | None) -> tuple[str, UUID | None]:
    """Parse ``new``, ``existing:{uuid}``, or empty (no selection)."""
    if raw is None:
        return "", None
    text = raw.strip()
    if text == "":
        return "", None
    if text == "new":
        return "new", None
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
    if settings.admin_preview_enabled and raw_token == PREVIEW_SESSION_TOKEN:
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
) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    actor_context = anonymous_actor_context(request)
    try:
        with db.db_connection(settings.database_url) as conn:
            with crm_transaction(conn):
                audit_service.record_login_failure(
                    conn,
                    actor_context=actor_context,
                    reason=reason,
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


def _try_claim_login_flow(
    request: Request,
    settings: Settings,
    csrf_token: str,
) -> bool:
    """Atomically claim a pre-auth login flow before credential verification.

    Consumption happens here (before password check) so exactly one concurrent
    submission can proceed; losers receive the same generic failure path as
    other invalid flows without running password verification.
    """
    raw_flow_token = admin_auth.read_login_flow_token(request)
    if raw_flow_token is None or not csrf_token:
        return False
    flow_hash = admin_auth.hash_session_token(raw_flow_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_token)
    now = datetime.now(timezone.utc)
    with db.db_connection(settings.database_url) as conn:
        row = db.claim_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            csrf_token_hash=csrf_hash,
            now=now,
        )
    return row is not None


def _try_burn_login_flow_cookie(request: Request, settings: Settings) -> bool:
    """Consume an unconsumed flow by cookie only (throttle or wrong CSRF)."""
    raw_flow_token = admin_auth.read_login_flow_token(request)
    if raw_flow_token is None:
        return False
    flow_hash = admin_auth.hash_session_token(raw_flow_token)
    now = datetime.now(timezone.utc)
    with db.db_connection(settings.database_url) as conn:
        return db.consume_admin_login_flow(
            conn,
            flow_token_hash=flow_hash,
            now=now,
        )


def _redirect_to_login_form(*, next_path: str | None) -> RedirectResponse:
    """Send the operator back to GET /admin/login when flow persistence fails."""
    login_url = "/admin/login"
    if next_path:
        login_url = f"{login_url}?next={quote(next_path, safe='')}"
    return RedirectResponse(url=login_url, status_code=303)


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

    * **Claim** — every POST first atomically claims the flow (cookie + CSRF +
      unconsumed + unexpired). Exactly one concurrent submission can claim;
      consumption happens at claim time before password verification.
    * **Invalid / lost claim** — failed claims burn the remaining unconsumed
      flow when CSRF differs and return a generic failure without verifying
      the password or minting a session.
    * **Invalid credentials** — successful claim with bad password issues a
      replacement flow cookie (``#153``); the consumed flow is not replayable.
    * **Rate limited** — keep the existing pre-auth flow (``#215``) and return
      the throttled message without claiming.
    * **Success** — clear the pre-auth flow cookie and issue the session cookie.
    """
    settings = get_settings()
    _require_admin_auth_configured(settings)
    normalized_username = username.strip()

    if not admin_auth.login_form_inputs_valid(
        username=username,
        password=password,
        csrf_token=csrf_token,
        login_flow_token=admin_auth.read_login_flow_token(request),
        next_path=next,
    ):
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf_token,
                error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
                next_path=next,
            ),
            status_code=400,
        )

    admission = admin_auth.try_admit_login_attempt(
        request, settings, username=normalized_username
    )
    if not admission.admitted:
        return HTMLResponse(
            admin_pages.render_admin_login_page(
                csrf_token=csrf_token,
                error_message=admin_auth.LOGIN_THROTTLED_MESSAGE,
                next_path=next,
            ),
            status_code=429,
        )

    try:
        claimed = _try_claim_login_flow(request, settings, csrf_token)
    except Exception:
        logger.exception("Failed to claim login flow")
        return _redirect_to_login_form(next_path=next)

    if not claimed:
        try:
            _try_burn_login_flow_cookie(request, settings)
        except Exception:
            logger.exception("Failed to burn login flow after failed claim")
            return _redirect_to_login_form(next_path=next)
        if admission.lockout_transition:
            _record_login_failure(
                request,
                reason="rate_limited",
            )
        else:
            _record_login_failure(request, reason="invalid_csrf")
        return _issue_login_flow_response(
            settings=settings,
            error_message=admin_auth.INVALID_CREDENTIALS_MESSAGE,
            next_path=next,
            status_code=400,
        )

    if not admin_auth.verify_admin_credentials(normalized_username, password, settings):
        if admission.lockout_transition:
            _record_login_failure(
                request,
                reason="rate_limited",
            )
        else:
            _record_login_failure(
                request,
                reason="invalid_credentials",
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
    csrf_token: str | None = Form(default=None, alias="csrf_token"),
) -> Response:
    settings = get_settings()
    _require_admin_auth_configured(settings)
    session = _load_valid_session(request, settings)
    if session is not None:
        if not csrf_token or not admin_auth.verify_session_csrf_request(
            request, csrf_token, settings
        ):
            raise HTTPException(status_code=400, detail=admin_auth.INVALID_REQUEST_MESSAGE)
        raw_token = admin_auth.read_session_token(request)
        if raw_token is not None:
            token_hash = admin_auth.hash_session_token(raw_token)
            with db.db_connection(settings.database_url) as conn:
                with crm_transaction(conn):
                    revoked = db.revoke_admin_session(conn, token_hash=token_hash)
                    if revoked:
                        audit_service.record_logout(
                            conn,
                            actor_context=actor_context_from_request(
                                request, actor=session.admin_username
                            ),
                            session_id=session.id,
                        )
        response = RedirectResponse(url="/admin/login", status_code=303)
        admin_auth.clear_session_cookie(response, settings)
        return response

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
        from app.admin_preview import build_preview_companies

        filters = {
            "q": q,
            "category": category if category in COMPANY_CATEGORIES else None,
            "stage": stage if stage in COMPANY_STAGES else None,
            "target_status": target_status if target_status in TARGET_STATUSES else None,
            "freshness": freshness if freshness in FRESHNESS_FILTERS else None,
            "archived": "1" if archived else None,
        }
        return HTMLResponse(
            company_pages.render_companies_list_page(
                companies=build_preview_companies(
                    query=filters["q"],
                    category=filters["category"],
                    stage=filters["stage"],
                    target_status=filters["target_status"],
                    freshness=filters["freshness"],
                    include_archived=archived,
                ),
                filters=filters,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
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
        result = _crm.create_company(
            conn,
            company=company,
            actor_context=actor_context_from_request(request, actor=session.admin_username),
        )
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
    if settings.admin_preview_enabled:
        if company_id in (
            PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
            PREVIEW_COMPANY_DETAIL_RESTORE_ID,
        ):
            company, contacts, records = build_preview_company_detail(company_id)
        else:
            company = build_preview_company(company_id)
            if company is None:
                raise HTTPException(status_code=404, detail="Company not found")
            contacts = build_preview_company_contacts(company_id)
            records = build_preview_company_research(company_id)
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
    with db.db_connection(settings.database_url) as conn:
        company = _crm.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        contacts = _crm.list_contacts_for_company(
            conn, company_id, include_archived=True
        )
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
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        company = build_preview_company(company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        error_message = error or warning
        if request.query_params.get("error") == "validation":
            error_message = PREVIEW_COMPANY_VALIDATION_ERROR
        return HTMLResponse(
            company_pages.render_company_form_page(
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                company=company,
                error_message=error_message,
            )
        )
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
        result = _crm.update_company(
            conn,
            company_id,
            company=company,
            actor_context=actor_context_from_request(request, actor=session.admin_username),
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Company not found")
    warnings = result["duplicate_warnings"]
    suffix = f"?warning={quote(f'{len(warnings)} possible domain duplicate(s)')}" if warnings else ""
    return RedirectResponse(url=f"/admin/companies/{company_id}/edit{suffix}", status_code=303)


@router.post("/companies/{company_id}/archive", response_model=None)
def admin_company_archive(request: Request, company_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    actor_context = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.archive_company(conn, company_id, actor_context=actor_context) is None:
            raise HTTPException(status_code=404, detail="Company not found")
    return RedirectResponse(url="/admin/companies", status_code=303)


@router.post("/companies/{company_id}/restore", response_model=None)
def admin_company_restore(request: Request, company_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    actor_context = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.restore_company(conn, company_id, actor_context=actor_context) is None:
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
            actor_context=actor_context_from_request(request, actor=session.admin_username),
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
        from app.admin_preview import build_preview_contacts

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
        contacts, companies = build_preview_contacts(
            query=filters["q"],
            company_id=parsed_company_id,
            buying_role=filters["buying_role"],
            include_archived=archived,
        )
        return HTMLResponse(
            contact_pages.render_contacts_list_page(
                contacts=contacts,
                companies=companies,
                filters=filters,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
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
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        return HTMLResponse(
            contact_pages.render_contact_form_page(
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                companies=build_preview_companies_for_select(),
            )
        )
    with db.db_connection(settings.database_url) as conn:
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
    crm_context_tags: list[str] = Form(default=[]),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        contact = ContactCreate(**_contact_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(url=f"/admin/contacts/new?error={quote(str(exc))}", status_code=303)
    try:
        with db.db_connection(get_settings().database_url) as conn:
            result = _crm.create_contact(
                conn,
                contact=contact,
                actor_context=actor_context_from_request(request, actor=session.admin_username),
            )
    except ContactEmailConflictError as exc:
        return RedirectResponse(url=f"/admin/contacts/new?error={quote(str(exc))}", status_code=303)
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
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        if contact_id in (
            PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
            PREVIEW_CONTACT_DETAIL_RESTORE_ID,
        ):
            contact, company, _records = build_preview_contact_detail(contact_id)
            companies = [company] if company is not None else []
        else:
            contact = build_preview_contact(contact_id)
            if contact is None:
                raise HTTPException(status_code=404, detail="Contact not found")
            companies = build_preview_companies_for_select()
        return HTMLResponse(
            contact_pages.render_contact_form_page(
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                companies=companies,
                contact=contact,
                error_message=error or warning,
            )
        )
    with db.db_connection(settings.database_url) as conn:
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
    crm_context_tags: list[str] = Form(default=[]),
) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    try:
        contact = ContactUpdate(**_contact_form_payload(**locals()))
    except (ValueError, TypeError, ValidationError) as exc:
        return RedirectResponse(
            url=f"/admin/contacts/{contact_id}/edit?error={quote(str(exc))}", status_code=303
        )
    try:
        with db.db_connection(get_settings().database_url) as conn:
            result = _crm.update_contact(
                conn,
                contact_id,
                contact=contact,
                actor_context=actor_context_from_request(request, actor=session.admin_username),
            )
    except ContactEmailConflictError as exc:
        return RedirectResponse(
            url=f"/admin/contacts/{contact_id}/edit?error={quote(str(exc))}", status_code=303
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    warnings = result["duplicate_warnings"]
    suffix = f"?warning={quote(f'{len(warnings)} possible duplicate(s)')}" if warnings else ""
    return RedirectResponse(url=f"/admin/contacts/{contact_id}/edit{suffix}", status_code=303)


@router.post("/contacts/{contact_id}/archive", response_model=None)
def admin_contact_archive(request: Request, contact_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    actor_context = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(get_settings().database_url) as conn:
        if _crm.archive_contact(conn, contact_id, actor_context=actor_context) is None:
            raise HTTPException(status_code=404, detail="Contact not found")
    return RedirectResponse(url="/admin/contacts", status_code=303)


@router.post("/contacts/{contact_id}/restore", response_model=None)
def admin_contact_restore(request: Request, contact_id: UUID, csrf_token: str = Form(...)) -> Response:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    actor_context = actor_context_from_request(request, actor=session.admin_username)
    csrf_token_for_forms = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled and contact_id == PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID:
        preview = preview_contact_restore_conflict()
        conflicting = ContactSafeSummary(**preview["conflicting_contact"])  # type: ignore[arg-type]
        return HTMLResponse(
            contact_pages.render_contact_restore_conflict_page(
                csrf_token=csrf_token_for_forms,
                admin_username=session.admin_username,
                archived_contact=preview["archived_contact"],  # type: ignore[arg-type]
                conflicting_contact=conflicting,
                company_name=str(preview["archived_contact"].get("company_name")),  # type: ignore[union-attr]
            )
        )

    with db.db_connection(settings.database_url) as conn:
        result = _crm.restore_contact(conn, contact_id, actor_context=actor_context)
        if result.outcome == "not_found":
            raise HTTPException(status_code=404, detail="Contact not found")
        if result.outcome == "conflict":
            return RedirectResponse(
                url=f"/admin/contacts/{contact_id}/restore-conflict",
                status_code=303,
            )
    return RedirectResponse(url=f"/admin/contacts/{contact_id}/edit", status_code=303)


@router.get("/contacts/{contact_id}/restore-conflict", response_class=HTMLResponse)
def admin_contact_restore_conflict(request: Request, contact_id: UUID) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled and contact_id == PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID:
        preview = preview_contact_restore_conflict()
        conflicting = ContactSafeSummary(**preview["conflicting_contact"])  # type: ignore[arg-type]
        return HTMLResponse(
            contact_pages.render_contact_restore_conflict_page(
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                archived_contact=preview["archived_contact"],  # type: ignore[arg-type]
                conflicting_contact=conflicting,
                company_name=str(preview["archived_contact"].get("company_name")),  # type: ignore[union-attr]
            )
        )

    with db.db_connection(settings.database_url) as conn:
        result = _crm.get_contact_restore_conflict(conn, contact_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Contact not found or no email conflict")
        company_name = None
        company_id = result.archived_contact.get("company_id") if result.archived_contact else None
        if company_id is not None:
            company = _crm.get_company(conn, UUID(str(company_id)))
            if company is not None:
                company_name = company.get("name")
        assert result.conflicting_contact is not None
        assert result.archived_contact is not None
        return HTMLResponse(
            contact_pages.render_contact_restore_conflict_page(
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                archived_contact=result.archived_contact,
                conflicting_contact=result.conflicting_contact,
                company_name=company_name,
            )
        )


@router.get("/contacts/{contact_id}", response_class=HTMLResponse)
def admin_contact_research(
    request: Request,
    contact_id: UUID,
    error: str | None = None,
) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        if contact_id in (
            PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
            PREVIEW_CONTACT_DETAIL_RESTORE_ID,
        ):
            contact, company, records = build_preview_contact_detail(contact_id)
        else:
            contact = build_preview_contact(contact_id)
            if contact is None:
                raise HTTPException(status_code=404, detail="Contact not found")
            company = None
            company_id = contact.get("company_id")
            if company_id is not None:
                company = build_preview_company(UUID(str(company_id)))
            records = build_preview_contact_research(contact_id)
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
            actor_context=actor_context_from_request(request, actor=session.admin_username),
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
            price_cents=effective_brief_price_cents(
                brief,
                list_price_cents=settings.brief_price_cents,
            ),
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
            price_cents=effective_brief_price_cents(
                brief,
                list_price_cents=settings.brief_price_cents,
            ),
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
    contact_choice: str = Form(default=""),
    acknowledge_archived_identity: str = Form(default=""),
    page: int = 1,
    q: str | None = Form(default=None),
    status: str | None = Form(default=None),
    date_from: str | None = Form(default=None),
    date_to: str | None = Form(default=None),
) -> RedirectResponse:
    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    parsed_brief_id = brief_service.parse_brief_id(brief_id)
    if parsed_brief_id is None:
        raise HTTPException(status_code=404, detail="Brief not found.")
    if not pipeline_capabilities_available(settings) and not settings.admin_preview_enabled:
        raise HTTPException(status_code=503, detail="Pipeline conversion is unavailable.")

    company_mode, selected_company_id = _parse_link_choice(company_choice)
    contact_mode, selected_contact_id = _parse_link_choice(contact_choice)
    acknowledge_archived = acknowledge_archived_identity == "1"
    detail_url = f"/admin/briefs/{parsed_brief_id}?converted=1"

    if settings.admin_preview_enabled:
        from app.admin_preview import preview_brief_convert_post

        error = preview_brief_convert_post(
            parsed_brief_id,
            company_mode=company_mode,
            contact_mode=contact_mode,
            selected_company_id=selected_company_id,
            selected_contact_id=selected_contact_id,
            acknowledge_archived_identity=acknowledge_archived,
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
                price_cents=effective_brief_price_cents(
                    brief,
                    list_price_cents=settings.brief_price_cents,
                ),
                company_choice=company_mode,
                contact_choice=contact_mode,
                selected_company_id=selected_company_id,
                selected_contact_id=selected_contact_id,
                acknowledge_archived_identity=acknowledge_archived,
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
    safe_page = max(page, 1)
    per_page = settings.audit_page_size
    db_error = False
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_audit_events

        all_events = build_preview_audit_events()
        total = len(all_events)
        start = (safe_page - 1) * per_page
        events = all_events[start : start + per_page]
    elif not settings.database_url:
        events, total = [], 0
    else:
        try:
            with db.db_connection(settings.database_url) as conn:
                events, total = audit_service.list_events(
                    conn,
                    page=safe_page,
                    per_page=per_page,
                )
        except Exception:
            logger.exception("Failed to load audit events")
            events, total = [], 0
            db_error = True
    return admin_html_response(
        admin_pages.render_admin_audit_page(
            admin_username=session.admin_username,
            events=events,
            page=safe_page,
            per_page=per_page,
            total=total,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/imports/batches", response_class=HTMLResponse)
def admin_import_batches_list(request: Request, page: int = 1) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    per_page = 50
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_import_batches

        batches, total = build_preview_import_batches()
        return HTMLResponse(
            admin_import_batches.render_import_batches_page(
                batches=batches,
                page=max(page, 1),
                per_page=per_page,
                total=total,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )
    if not settings.database_url:
        batches, total = [], 0
    else:
        with db.db_connection(settings.database_url) as conn:
            batches, total = _crm.list_import_batches(conn, page=page, per_page=per_page)
    return HTMLResponse(
        admin_import_batches.render_import_batches_page(
            batches=batches,
            page=max(page, 1),
            per_page=per_page,
            total=total,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        )
    )


@router.get("/imports/batches/{batch_id}", response_class=HTMLResponse)
def admin_import_batch_detail(request: Request, batch_id: UUID) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_import_batch_detail

        state = build_preview_import_batch_detail(str(batch_id))
        if state is None:
            return HTMLResponse(
                admin.render_admin_not_found(
                    f"/admin/imports/batches/{batch_id}",
                    admin_username=session.admin_username,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        return HTMLResponse(
            admin_import_batches.render_import_batch_detail_page(
                batch=state["batch"],
                rows=state["rows"],
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )
    if not settings.database_url:
        return HTMLResponse(
            admin.render_admin_not_found(
                f"/admin/imports/batches/{batch_id}",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    with db.db_connection(settings.database_url) as conn:
        state = _crm.get_import_batch(conn, batch_id)
    if state is None:
        return HTMLResponse(
            admin.render_admin_not_found(
                f"/admin/imports/batches/{batch_id}",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    return HTMLResponse(
        admin_import_batches.render_import_batch_detail_page(
            batch=state["batch"],
            rows=state["rows"],
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        )
    )


@router.post("/imports/batches/{batch_id}/rollback")
def admin_import_batch_rollback(
    request: Request,
    batch_id: UUID,
    csrf_token: str = Form(...),
) -> Response:
    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)
    detail_url = f"/admin/imports/batches/{batch_id}"
    if settings.admin_preview_enabled:
        return RedirectResponse(url=detail_url, status_code=303)
    if not settings.database_url:
        return RedirectResponse(
            url=f"{detail_url}?error={quote('Database unavailable')}",
            status_code=303,
        )
    try:
        with db.db_connection(settings.database_url) as conn:
            _crm.rollback_import_batch(
                conn,
                actor_context=actor_context_from_request(request, actor=session.admin_username),
                batch_id=batch_id,
            )
    except ValueError as exc:
        return RedirectResponse(
            url=f"{detail_url}?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url=detail_url, status_code=303)


@router.post("/api/imports/linkedin/commit")
async def admin_linkedin_import_commit(request: Request) -> JSONResponse:
    session = require_admin_session(request)
    settings = get_settings()
    from app.admin_json_request import (
        read_bounded_json_object,
        read_session_csrf_header,
        reject_duplicate_csrf_field,
        verify_session_csrf_header_or_reject,
    )
    from app.admin_linkedin_commit import (
        LINKEDIN_COMMIT_MAX_BODY_BYTES,
        LINKEDIN_COMMIT_MAX_CONNECTIONS,
        LINKEDIN_COMMIT_MAX_MESSAGE_METADATA,
    )

    verify_session_csrf_header_or_reject(
        request,
        settings,
        submitted_csrf_token=read_session_csrf_header(request),
    )
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database unavailable")
    payload = await read_bounded_json_object(
        request,
        max_body_bytes=LINKEDIN_COMMIT_MAX_BODY_BYTES,
    )
    reject_duplicate_csrf_field(payload)
    connections = payload.get("connections")
    if not isinstance(connections, list):
        raise HTTPException(status_code=400, detail="connections must be a list")
    if len(connections) > LINKEDIN_COMMIT_MAX_CONNECTIONS:
        raise HTTPException(status_code=400, detail="connections list too large")
    message_metadata = payload.get("message_metadata")
    if message_metadata is not None:
        if not isinstance(message_metadata, list):
            raise HTTPException(status_code=400, detail="message_metadata must be a list")
        if len(message_metadata) > LINKEDIN_COMMIT_MAX_MESSAGE_METADATA:
            raise HTTPException(status_code=400, detail="message_metadata list too large")
    owner_name = payload.get("owner_name")
    if owner_name is not None and not isinstance(owner_name, str):
        raise HTTPException(status_code=400, detail="owner_name must be a string")
    with db.db_connection(settings.database_url) as conn:
        result = _crm.commit_linkedin_import(
            conn,
            actor_context=actor_context_from_request(request, actor=session.admin_username),
            connections=connections,
            export_date=payload.get("export_date"),
            checksum=payload.get("checksum"),
            message_metadata=message_metadata,
            owner_name=owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else None,
        )
    batch = result["batch"]
    return JSONResponse(
        {
            "batch_id": str(batch["id"]),
            "idempotent": result["idempotent"],
            "status": batch.get("status"),
            "summary_counts": result.get("summary_counts"),
            "checksum": batch.get("checksum"),
        }
    )


@router.get("/imports", response_class=HTMLResponse)
def admin_imports(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import render_preview_imports_main

        return HTMLResponse(
            render_admin_shell(
                title="Imports",
                main=render_preview_imports_main(),
                active_path="/admin/imports",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            )
        )
    return HTMLResponse(
        import_pages.render_imports_page(
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            csp_nonce=csp_nonce_from_request(request),
        )
    )



@router.post("/imports/reconcile-preview")
async def admin_imports_reconcile_preview(request: Request) -> JSONResponse:
    """Server-side incremental reconciliation preview for parsed LinkedIn connections."""
    require_admin_session(request)
    settings = get_settings()
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_linkedin_reconcile

        return JSONResponse(build_preview_linkedin_reconcile())
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc
    connections = body.get("connections")
    if not isinstance(connections, list):
        raise HTTPException(status_code=400, detail="connections must be a list.")
    with db.db_connection(settings.database_url) as conn:
        preview = _crm.preview_linkedin_reconcile(conn, connections=connections)
    return JSONResponse(preview)


for _link in ADMIN_NAV_LINKS:
    if _link["href"] in {
        "/admin",
        "/admin/audit",
        "/admin/briefs",
        "/admin/companies",
        "/admin/contacts",
        "/admin/imports",
        "/admin/pipeline",
        "/admin/signals",
        "/admin/discovery",
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
def admin_dashboard(request: Request) -> Response:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_acquisition_dashboard_data

        try:
            preview_data = build_preview_acquisition_dashboard_data()
        except Exception:
            logger.exception("Failed to build preview acquisition dashboard")
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return HTMLResponse(
            admin_dashboard_pages.render_acquisition_dashboard_page(
                data=preview_data,
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
