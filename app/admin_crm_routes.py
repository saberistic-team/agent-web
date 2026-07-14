"""Protected admin routes for CRM contacts and companies."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import admin, admin_companies, admin_contacts, db
from app.admin_deps import (
    issue_session_csrf,
    require_admin_session,
    verify_session_csrf,
)
from app.config import Settings, get_settings
from app.crm_service import CrmService

router = APIRouter(tags=["admin-crm"])


def _crm_service() -> CrmService:
    return CrmService()


def _require_crm_db(settings: Settings) -> None:
    if not settings.database_configured:
        raise HTTPException(status_code=503, detail="Database not configured")


@router.get("/contacts", response_class=HTMLResponse)
def admin_contacts_list(request: Request) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    query = request.query_params.get("q", "")
    include_archived = request.query_params.get("include_archived") == "1"
    csrf_token = issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        contacts = _crm_service().search_contacts(
            conn,
            query=query,
            include_archived=include_archived,
        )
    return HTMLResponse(
        admin_contacts.render_contacts_list_page(
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
    csrf_token = issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        companies = _crm_service()._repos.companies.list_all(conn)
    contact: dict[str, object] = {}
    if company_id:
        contact["company_id"] = company_id
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
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
    verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = issue_session_csrf(settings, session.id)
    if not name.strip():
        with db.db_connection(settings.database_url) as conn:
            companies = _crm_service()._repos.companies.list_all(conn)
        return HTMLResponse(
            admin_contacts.render_contact_form_page(
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
    csrf_token = issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        contact = _crm_service().get_contact_with_roles(conn, UUID(contact_id))
        companies = _crm_service()._repos.companies.list_all(conn)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return HTMLResponse(
        admin_contacts.render_contact_form_page(
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
    verify_session_csrf(csrf_token, session)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = issue_session_csrf(settings, session.id)
    if not name.strip():
        with db.db_connection(settings.database_url) as conn:
            companies = _crm_service()._repos.companies.list_all(conn)
            contact = _crm_service().get_contact_with_roles(conn, UUID(contact_id))
        return HTMLResponse(
            admin_contacts.render_contact_form_page(
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
    verify_session_csrf(csrf_token, session)
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
    verify_session_csrf(csrf_token, session)
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
    csrf_token = issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        companies = _crm_service()._repos.companies.list_all(conn)
    return HTMLResponse(
        admin_companies.render_companies_list_page(
            companies=companies,
            csrf_token=csrf_token,
        )
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def admin_company_detail(request: Request, company_id: str) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _require_crm_db(settings)
    csrf_token = issue_session_csrf(settings, session.id)
    with db.db_connection(settings.database_url) as conn:
        company = _crm_service()._repos.companies.get_by_id(conn, UUID(company_id))
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        contacts = _crm_service().list_company_contacts(conn, UUID(company_id))
    return HTMLResponse(
        admin_contacts.render_company_detail_page(
            company=company,
            contacts=contacts,
            csrf_token=csrf_token,
        )
    )


@router.get("/{section}", response_class=HTMLResponse)
def admin_section(request: Request, section: str) -> HTMLResponse:
    """Render placeholder pages for deferred admin sections."""
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = issue_session_csrf(settings, session.id)
    path = f"/admin/{section}"
    if not admin.is_admin_path(path):
        return HTMLResponse(
            admin.render_admin_not_found(path, csrf_token=csrf_token),
            status_code=404,
        )
    return HTMLResponse(admin.render_admin_page(path, csrf_token=csrf_token))
