"""Qualification target list admin routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import db
from app.admin_qualification_pages import render_target_detail_page, render_targets_list_page
from app.config import get_settings
from app.crm_service import CrmService
from app.qualification_targets import QualificationTargetFilters, WorkingListCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/targets", tags=["admin-targets"])
_crm = CrmService()


def _parse_filters(
    *,
    tier: str | None = None,
    category: str | None = None,
    stage: str | None = None,
    pipeline_stage: str | None = None,
    owner: str | None = None,
    freshness: str | None = None,
    warm_path: str | None = None,
) -> tuple[QualificationTargetFilters | None, dict[str, str | None]]:
    raw = {
        "tier": tier,
        "category": category,
        "stage": stage,
        "pipeline_stage": pipeline_stage,
        "owner": owner,
        "freshness": freshness,
        "warm_path": warm_path,
    }
    try:
        filters = QualificationTargetFilters.model_validate(raw)
    except ValidationError:
        filters = QualificationTargetFilters()
        raw = {key: None for key in raw}
    has_filter = any(value for value in raw.values())
    return (filters if has_filter else None, raw)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_targets_list(
    request: Request,
    tier: str | None = None,
    category: str | None = None,
    stage: str | None = None,
    pipeline_stage: str | None = None,
    owner: str | None = None,
    freshness: str | None = None,
    warm_path: str | None = None,
    saved: str | None = None,
) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    filters, filter_values = _parse_filters(
        tier=tier,
        category=category,
        stage=stage,
        pipeline_stage=pipeline_stage,
        owner=owner,
        freshness=freshness,
        warm_path=warm_path,
    )

    if settings.admin_preview_enabled:
        from app.admin_preview import (
            build_preview_qualification_targets,
            build_preview_qualification_working_lists,
        )

        return HTMLResponse(
            render_targets_list_page(
                targets=build_preview_qualification_targets(filters=filter_values),
                filters=filter_values,
                working_lists=build_preview_qualification_working_lists(),
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
                save_message="Working list saved." if saved else None,
            )
        )

    targets: list[dict] = []
    working_lists: list[dict] = []
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                targets = _crm.list_qualification_targets(
                    conn,
                    filters=filters,
                    actor=session.admin_username,
                    persist_scores=True,
                )
                working_lists = _crm.list_qualification_working_lists(
                    conn, owner=session.admin_username
                )
        except Exception:
            logger.exception("Failed to load qualification targets")

    return HTMLResponse(
        render_targets_list_page(
            targets=targets,
            filters=filter_values,
            working_lists=working_lists,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
            save_message="Working list saved." if saved else None,
        )
    )


@router.get("/{company_id}", response_class=HTMLResponse)
def admin_target_detail(request: Request, company_id: UUID) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import (
            build_preview_qualification_target_detail,
            preview_qualification_target_exists,
        )

        if not preview_qualification_target_exists(company_id):
            raise HTTPException(status_code=404, detail="Company not found")
        company, target, history = build_preview_qualification_target_detail(company_id)
        return HTMLResponse(
            render_target_detail_page(
                company=company,
                target=target,
                tier_history=history,
                csrf_token=csrf_token,
                admin_username=session.admin_username,
                preview_banner="Preview data — not production",
            )
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    with db.db_connection(settings.database_url) as conn:
        company = _crm.get_company(conn, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        targets = _crm.list_qualification_targets(
            conn,
            actor=session.admin_username,
            persist_scores=False,
        )
        target = next(
            (row for row in targets if str(row.get("company_id")) == str(company_id)),
            None,
        )
        history = _crm.list_qualification_tier_history(conn, company_id)

    return HTMLResponse(
        render_target_detail_page(
            company=company,
            target=target,
            tier_history=history,
            csrf_token=csrf_token,
            admin_username=session.admin_username,
        )
    )


@router.post("/working-list")
def admin_save_working_list(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    company_ids: list[str] = Form(default=[]),
) -> RedirectResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        _verify_session_csrf,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, settings, csrf_token, _session_csrf_for_forms(request, settings))

    if settings.admin_preview_enabled:
        return RedirectResponse(url="/admin/targets?saved=1", status_code=303)

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        payload = WorkingListCreate(name=name, company_ids=company_ids)
    except ValidationError as exc:
        logger.warning("Invalid working list payload: %s", exc)
        return RedirectResponse(url="/admin/targets", status_code=303)

    with db.db_connection(settings.database_url) as conn:
        _crm.save_qualification_working_list(
            conn,
            owner=session.admin_username,
            payload=payload,
        )

    return RedirectResponse(url="/admin/targets?saved=1", status_code=303)
