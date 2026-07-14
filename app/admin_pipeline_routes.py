"""Authenticated admin JSON routes for acquisition pipeline operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import db
from app.admin_routes import require_admin_session
from app.config import get_settings
from app.crm_service import (
    ConfirmRequiredError,
    CrmService,
    InvalidStageError,
    InvalidTransitionError,
    ReasonRequiredError,
)
from app.pipeline import PIPELINE_STAGES, PIPELINE_ACTIVITY_TYPES

router = APIRouter(prefix="/admin/api/pipeline", tags=["admin-pipeline"])


class StageTransitionRequest(BaseModel):
    to_stage: str
    reason: str | None = None
    confirm: bool = False


class NextActionUpdateRequest(BaseModel):
    next_action: str | None = None
    due_at: datetime | None = None
    owner: str | None = None
    expected_value: float | None = None
    clear_due_at: bool = False


class ActivityCreateRequest(BaseModel):
    activity_type: str
    summary: str
    contact_id: UUID | None = None
    metadata: dict[str, Any] | None = None


def _crm_service() -> CrmService:
    return CrmService()


def _with_db_conn():
    settings = get_settings()
    return db.db_connection(settings.database_url)


@router.get("/stages")
def list_pipeline_stages(request: Request) -> dict[str, Any]:
    require_admin_session(request)
    return {"stages": list(PIPELINE_STAGES)}


@router.get("/activity-types")
def list_activity_types(request: Request) -> dict[str, Any]:
    require_admin_session(request)
    return {"activity_types": list(PIPELINE_ACTIVITY_TYPES)}


@router.get("/companies")
def list_companies(
    request: Request,
    stage: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    require_admin_session(request)
    with _with_db_conn() as conn:
        companies = _crm_service().list_companies_by_stage(
            conn,
            pipeline_stage=stage,
            limit=limit,
        )
    return {"companies": companies}


@router.get("/companies/{company_id}")
def get_company_detail(request: Request, company_id: UUID) -> dict[str, Any]:
    require_admin_session(request)
    with _with_db_conn() as conn:
        detail = _crm_service().get_company_pipeline_detail(conn, company_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return detail


@router.post("/companies/{company_id}/stage")
def transition_stage(
    request: Request,
    company_id: UUID,
    body: StageTransitionRequest,
) -> dict[str, Any]:
    session = require_admin_session(request)
    try:
        with _with_db_conn() as conn:
            result = _crm_service().transition_company_stage(
                conn,
                company_id=company_id,
                to_stage=body.to_stage,
                actor=session.admin_username,
                reason=body.reason,
                confirm=body.confirm,
            )
    except ReasonRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfirmRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidTransitionError, InvalidStageError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.patch("/companies/{company_id}/next-action")
def update_next_action(
    request: Request,
    company_id: UUID,
    body: NextActionUpdateRequest,
) -> dict[str, Any]:
    session = require_admin_session(request)
    try:
        with _with_db_conn() as conn:
            company = _crm_service().update_company_next_action(
                conn,
                company_id=company_id,
                actor=session.admin_username,
                next_action=body.next_action,
                due_at=body.due_at,
                owner=body.owner,
                expected_value=body.expected_value,
                clear_due_at=body.clear_due_at,
            )
    except InvalidStageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"company": company}


@router.post("/companies/{company_id}/activities")
def record_activity(
    request: Request,
    company_id: UUID,
    body: ActivityCreateRequest,
) -> dict[str, Any]:
    session = require_admin_session(request)
    try:
        with _with_db_conn() as conn:
            activity = _crm_service().record_activity_for_company(
                conn,
                company_id=company_id,
                activity_type=body.activity_type,
                summary=body.summary,
                contact_id=body.contact_id,
                metadata=body.metadata,
                actor=session.admin_username,
            )
    except InvalidStageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"activity": activity}


@router.get("/actions/overdue")
def list_overdue_actions(
    request: Request,
    as_of: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    require_admin_session(request)
    reference = as_of or datetime.now(timezone.utc)
    with _with_db_conn() as conn:
        companies = _crm_service().list_overdue_actions(conn, as_of=reference, limit=limit)
    return {"as_of": reference.isoformat(), "companies": companies}


@router.get("/actions/upcoming")
def list_upcoming_actions(
    request: Request,
    as_of: datetime | None = Query(default=None),
    within_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    require_admin_session(request)
    reference = as_of or datetime.now(timezone.utc)
    with _with_db_conn() as conn:
        companies = _crm_service().list_upcoming_actions(
            conn,
            as_of=reference,
            within_days=within_days,
            limit=limit,
        )
    return {
        "as_of": reference.isoformat(),
        "within_days": within_days,
        "companies": companies,
    }
