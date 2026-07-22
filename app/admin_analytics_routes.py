"""Marketing analytics admin routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import db
from app.admin_analytics_pages import (
    render_analytics_dashboard_csv,
    render_analytics_dashboard_page,
)
from app.analytics_dashboard import (
    empty_analytics_dashboard,
    load_analytics_dashboard,
)
from app.config import get_settings
from app.repositories.postgres import get_repositories

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_analytics_dashboard(
    request: Request,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> Response:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        try:
            preview_data = build_preview_analytics_dashboard_data(
                date_from=date_from,
                date_to=date_to,
            )
        except Exception:
            logger.exception("Failed to build preview analytics dashboard")
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return HTMLResponse(
            render_analytics_dashboard_page(
                data=preview_data,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    db_error = False
    dashboard_data = empty_analytics_dashboard(
        date_from=date_from,
        date_to=date_to,
    )
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_analytics_dashboard(
                    conn,
                    get_repositories().analytics_dashboard,
                    date_from=date_from,
                    date_to=date_to,
                )
        except Exception:
            logger.exception("Failed to load analytics dashboard")
            db_error = True

    return HTMLResponse(
        render_analytics_dashboard_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/export.csv")
def admin_analytics_export(
    request: Request,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> Response:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        data = build_preview_analytics_dashboard_data(
            date_from=date_from,
            date_to=date_to,
        )
    elif not settings.database_url:
        data = empty_analytics_dashboard(
            date_from=date_from,
            date_to=date_to,
        )
    else:
        try:
            with db.db_connection(settings.database_url) as conn:
                data = load_analytics_dashboard(
                    conn,
                    get_repositories().analytics_dashboard,
                    date_from=date_from,
                    date_to=date_to,
                )
        except Exception:
            logger.exception("Failed to export analytics dashboard")
            return JSONResponse(
                {"detail": "Analytics export temporarily unavailable."},
                status_code=503,
            )

    filename = (
        f"marketing-analytics-{data.date_range.from_raw}-{data.date_range.to_raw}.csv"
    )
    return Response(
        content=render_analytics_dashboard_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
