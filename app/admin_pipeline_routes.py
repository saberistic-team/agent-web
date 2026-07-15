"""Acquisition pipeline admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import db
from app.acquisition_pipeline import (
    PipelineActivityCreate,
    PipelineNextActionUpdate,
    PipelineStageChange,
    PipelineTransitionError,
)
from app.admin_pipeline_pages import render_pipeline_detail_page, render_pipeline_list_page
from app.actor_context import actor_context_from_request
from app.config import get_settings
from app.crm_service import CrmService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/pipeline", tags=["admin-pipeline"])
_crm = CrmService()


def _parse_due_at(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_pipeline_list(request: Request, stage: str | None = None) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_pipeline_companies

        return HTMLResponse(
            render_pipeline_list_page(
                companies=build_preview_pipeline_companies(stage_filter=stage),
                stage_filter=stage,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    companies: list[dict] = []
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                companies = _crm.list_pipeline_companies(
                    conn, pipeline_stage=stage or None
                )
        except Exception:
            logger.exception("Failed to load pipeline companies")

    return HTMLResponse(
        render_pipeline_list_page(
            companies=companies,
            stage_filter=stage,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.get("/{company_id}", response_class=HTMLResponse)
def admin_pipeline_detail(request: Request, company_id: UUID) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_pipeline_detail

        detail = build_preview_pipeline_detail(company_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Company not found")
        company, history, activities = detail
        return HTMLResponse(
            render_pipeline_detail_page(
                company=company,
                history=history,
                activities=activities,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    with db.db_connection(settings.database_url) as conn:
        company = _crm.get_pipeline_company(conn, company_id)
        if company is None or not company.get("pipeline_stage"):
            raise HTTPException(status_code=404, detail="Company not in pipeline")
        history = _crm.list_pipeline_stage_history(conn, company_id)
        activities = _crm._repos.activities.list_for_company(conn, company_id)

    return HTMLResponse(
        render_pipeline_detail_page(
            company=company,
            history=history,
            activities=activities,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.post("/{company_id}/stage")
def admin_pipeline_stage_change(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
    to_stage: str = Form(...),
    loss_reason: str | None = Form(None),
    nurture_reason: str | None = Form(None),
    confirm: str | None = Form(None),
) -> RedirectResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        _verify_session_csrf,
        require_admin_session,
    )

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        change = PipelineStageChange(
            to_stage=to_stage,
            confirm=confirm == "1",
            loss_reason=loss_reason,
            nurture_reason=nurture_reason,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0]["msg"])) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    try:
        with db.db_connection(settings.database_url) as conn:
            _crm.transition_pipeline_stage(
                conn,
                actor_context=actor,
                company_id=company_id,
                change=change,
            )
    except PipelineTransitionError as exc:
        with db.db_connection(settings.database_url) as conn:
            company = _crm.get_pipeline_company(conn, company_id)
            history = _crm.list_pipeline_stage_history(conn, company_id)
            activities = _crm._repos.activities.list_for_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found") from exc
        return HTMLResponse(
            render_pipeline_detail_page(
                company=company,
                history=history,
                activities=activities,
                csrf_token=_session_csrf_for_forms(request, settings),
                admin_username=session.admin_username,
                error_message=str(exc),
            ),
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)


@router.post("/{company_id}/next-action")
def admin_pipeline_next_action(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
    next_action: str | None = Form(None),
    next_action_due_at: str | None = Form(None),
    pipeline_owner: str | None = Form(None),
    expected_value_cents: str | None = Form(None),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    parsed_value: int | None = None
    if expected_value_cents and expected_value_cents.strip():
        parsed_value = int(expected_value_cents.strip())

    try:
        update = PipelineNextActionUpdate(
            next_action=next_action,
            next_action_due_at=_parse_due_at(next_action_due_at),
            pipeline_owner=pipeline_owner,
            expected_value_cents=parsed_value,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0]["msg"])) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.update_pipeline_next_action(
            conn,
            actor_context=actor,
            company_id=company_id,
            update=update,
        )
    return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)


@router.post("/{company_id}/activities")
def admin_pipeline_activity(
    request: Request,
    company_id: UUID,
    csrf_token: str = Form(...),
    activity_type: str = Form(...),
    summary: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        activity = PipelineActivityCreate(activity_type=activity_type, summary=summary)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0]["msg"])) from exc

    with db.db_connection(settings.database_url) as conn:
        _crm.record_pipeline_activity(conn, company_id=company_id, activity=activity)
    return RedirectResponse(url=f"/admin/pipeline/{company_id}", status_code=303)
