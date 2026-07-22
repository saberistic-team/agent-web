"""Daily acquisition action queue admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app import db
from app.acquisition_action_queue import load_action_queue
from app.acquisition_pipeline import PipelineNextActionUpdate
from app.admin_action_queue_pages import render_action_queue_page
from app.actor_context import actor_context_from_request
from app.config import get_settings
from app.crm_export import render_acquisition_export_csv
from app.crm_service import CrmService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/queue", tags=["admin-queue"])
_crm = CrmService()


def _parse_due_at(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _queue_redirect(*, message: str | None = None) -> RedirectResponse:
    url = "/admin/queue"
    if message:
        from urllib.parse import quote

        url = f"{url}?msg={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_action_queue(request: Request) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    action_message = request.query_params.get("msg")

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_action_queue_data

        return HTMLResponse(
            render_action_queue_page(
                data=build_preview_action_queue_data(),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
                action_message=action_message,
            )
        )

    queue_data = None
    db_error = False
    if settings.database_url:
        try:
            from app.repositories.postgres import get_repositories

            with db.db_connection(settings.database_url) as conn:
                queue_data = load_action_queue(conn, get_repositories().action_queue)
        except Exception:
            logger.exception("Failed to load action queue")
            db_error = True

    if queue_data is None:
        from app.acquisition_action_queue import ActionQueueData

        queue_data = ActionQueueData(items=(), generated_at=datetime.now(timezone.utc))

    return HTMLResponse(
        render_action_queue_page(
            data=queue_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
            action_message=action_message,
        )
    )


@router.get("/export.csv")
def admin_action_queue_export(request: Request) -> Response:
    from app.admin_routes import require_admin_session
    from app.repositories.postgres import get_repositories

    session = require_admin_session(request)
    settings = get_settings()
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_export_csv

        return Response(
            content=build_preview_export_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="acquisition-export.csv"'},
        )
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.request_export(
            conn,
            actor_context=actor,
            export_type="acquisition_queue_csv",
            filters={"source": "action_queue"},
        )
        csv_text = render_acquisition_export_csv(conn, get_repositories().action_queue)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="acquisition-export.csv"'},
    )


@router.post("/complete")
def admin_queue_complete(
    request: Request,
    csrf_token: str = Form(...),
    company_id: UUID = Form(...),
    item_key: str = Form(...),
    item_category: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return _queue_redirect(message="Preview: action recorded.")
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.complete_queue_item(
            conn,
            actor_context=actor,
            company_id=company_id,
            item_key=item_key,
            item_category=item_category,
        )
    return _queue_redirect(message="Action completed.")


@router.post("/snooze")
def admin_queue_snooze(
    request: Request,
    csrf_token: str = Form(...),
    company_id: UUID = Form(...),
    item_key: str = Form(...),
    item_category: str = Form(...),
    snooze_days: int = Form(3),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return _queue_redirect(message="Preview: snoozed.")
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.snooze_queue_item(
            conn,
            actor_context=actor,
            company_id=company_id,
            item_key=item_key,
            item_category=item_category,
            snooze_days=snooze_days,
        )
    return _queue_redirect(message=f"Snoozed {snooze_days} day(s).")


@router.post("/reschedule")
def admin_queue_reschedule(
    request: Request,
    csrf_token: str = Form(...),
    company_id: UUID = Form(...),
    item_key: str = Form(...),
    item_category: str = Form(...),
    next_action_due_at: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return _queue_redirect(message="Preview: rescheduled.")
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    due_at = _parse_due_at(next_action_due_at)
    if due_at is None:
        raise HTTPException(status_code=400, detail="Invalid due date.")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.reschedule_queue_item(
            conn,
            actor_context=actor,
            company_id=company_id,
            item_key=item_key,
            item_category=item_category,
            next_action_due_at=due_at,
        )
    return _queue_redirect(message="Rescheduled.")


@router.post("/replace")
def admin_queue_replace(
    request: Request,
    csrf_token: str = Form(...),
    company_id: UUID = Form(...),
    item_key: str = Form(...),
    item_category: str = Form(...),
    next_action: str = Form(...),
    next_action_due_at: str = Form(...),
) -> RedirectResponse:
    from app.admin_routes import _verify_session_csrf, require_admin_session

    session = require_admin_session(request)
    _verify_session_csrf(request, session, csrf_token)
    settings = get_settings()
    if settings.admin_preview_enabled:
        return _queue_redirect(message="Preview: next action replaced.")
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    due_at = _parse_due_at(next_action_due_at)
    if due_at is None:
        raise HTTPException(status_code=400, detail="Invalid due date.")

    try:
        update = PipelineNextActionUpdate(
            next_action=next_action,
            next_action_due_at=due_at,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0]["msg"])) from exc

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.replace_queue_item(
            conn,
            actor_context=actor,
            company_id=company_id,
            item_key=item_key,
            item_category=item_category,
            update=update,
        )
    return _queue_redirect(message="Next action replaced.")
