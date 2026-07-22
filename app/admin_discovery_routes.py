"""Admin routes for discovery run history and manual triggers."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import admin, admin_auth, db
from app.admin_discovery import render_discovery_run_detail_page, render_discovery_runs_page
from app.actor_context import actor_context_from_request, correlation_id_from_request
from app.admin_response import admin_html_response
from app.config import Settings, get_settings
from app.discovery.service import get_discovery_run_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/discovery", tags=["admin-discovery"])


def _session_csrf_for_forms(request: Request, settings: Settings) -> str:
    return admin_auth.session_csrf_for_request(request, settings)


def _verify_session_csrf(
    request: Request,
    session: admin_auth.AdminSession,
    csrf_token: str,
) -> None:
    settings = get_settings()
    if not admin_auth.verify_session_csrf_request(request, csrf_token, settings):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def require_admin_session(request: Request) -> admin_auth.AdminSession:
    from app.admin_routes import require_admin_session as _require

    return _require(request)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_discovery_runs(request: Request, page: int = 1) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    per_page = 50
    trigger_message = request.query_params.get("message")

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_discovery_runs

        runs, total = build_preview_discovery_runs()
        return admin_html_response(
            render_discovery_runs_page(
                runs=runs,
                page=max(page, 1),
                per_page=per_page,
                total=total,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                schedule_interval_days=settings.discovery_schedule_interval_days,
                preview_banner="Preview data — not production",
                trigger_message=trigger_message,
            )
        )

    if not settings.database_url:
        runs, total = [], 0
    else:
        service = get_discovery_run_service()
        with db.db_connection(settings.database_url) as conn:
            runs, total = service.list_runs(conn, page=page, per_page=per_page)

    return admin_html_response(
        render_discovery_runs_page(
            runs=runs,
            page=max(page, 1),
            per_page=per_page,
            total=total,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            schedule_interval_days=settings.discovery_schedule_interval_days,
            trigger_message=trigger_message,
        )
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def admin_discovery_run_detail(request: Request, run_id: UUID) -> HTMLResponse:
    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_discovery_run_detail

        state = build_preview_discovery_run_detail(str(run_id))
        if state is None:
            return HTMLResponse(
                admin.render_admin_not_found(
                    f"/admin/discovery/runs/{run_id}",
                    admin_username=session.admin_username,
                    csrf_token=csrf_token,
                ),
                status_code=404,
            )
        return admin_html_response(
            render_discovery_run_detail_page(
                run=state["run"],
                sources=state["sources"],
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    if not settings.database_url:
        return HTMLResponse(
            admin.render_admin_not_found(
                f"/admin/discovery/runs/{run_id}",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )

    service = get_discovery_run_service()
    with db.db_connection(settings.database_url) as conn:
        state = service.get_run(conn, run_id)
    if state is None:
        return HTMLResponse(
            admin.render_admin_not_found(
                f"/admin/discovery/runs/{run_id}",
                admin_username=session.admin_username,
                csrf_token=csrf_token,
            ),
            status_code=404,
        )
    return admin_html_response(
        render_discovery_run_detail_page(
            run=state["run"],
            sources=state["sources"],
            admin_username=session.admin_username,
            csrf_token=csrf_token,
        )
    )


@router.post("/run")
def admin_discovery_run_trigger(
    request: Request,
    csrf_token: str = Form(..., alias="csrf_token"),
) -> RedirectResponse:
    session = require_admin_session(request)
    settings = get_settings()
    _verify_session_csrf(request, session, csrf_token)

    if settings.admin_preview_enabled:
        return RedirectResponse(
            url="/admin/discovery?message=Preview+mode+%E2%80%94+runs+are+read-only",
            status_code=303,
        )

    if not settings.database_url:
        return RedirectResponse(
            url="/admin/discovery?message=Database+is+not+configured",
            status_code=303,
        )

    actor_context = actor_context_from_request(request, actor=session.admin_username)
    correlation_id = correlation_id_from_request(request)
    service = get_discovery_run_service()
    try:
        with db.db_connection(settings.database_url) as conn:
            result = service.trigger_manual_run(
                conn,
                settings,
                actor=actor_context.actor,
                correlation_id=correlation_id,
            )
    except Exception:
        logger.exception("Manual discovery run failed")
        return RedirectResponse(
            url="/admin/discovery?message=Discovery+run+failed",
            status_code=303,
        )

    if result.run_id is not None:
        return RedirectResponse(
            url=f"/admin/discovery/runs/{result.run_id}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/discovery?message=Discovery+run+did+not+start",
        status_code=303,
    )
