"""Marketing analytics admin dashboard routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.config import get_settings
from app.marketing_analytics_dashboard import (
    MarketingAnalyticsDashboardData,
    load_marketing_analytics_dashboard,
    parse_analytics_date_range,
    render_analytics_export_csv,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_marketing_analytics(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    from app.admin_analytics_pages import render_marketing_analytics_page
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    date_range = parse_analytics_date_range(date_from=date_from, date_to=date_to)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        return HTMLResponse(
            render_marketing_analytics_page(
                data=build_preview_marketing_analytics_data(date_range=date_range),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    dashboard_data: MarketingAnalyticsDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
            from app.repositories.postgres import get_repositories

            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_marketing_analytics_dashboard(
                    conn,
                    get_repositories().marketing_analytics,
                    date_range=date_range,
                )
        except Exception:
            logger.exception("Failed to load marketing analytics dashboard")
            db_error = True

    if dashboard_data is None:
        from app.marketing_analytics_dashboard import empty_dashboard_data

        dashboard_data = empty_dashboard_data(date_range)

    return HTMLResponse(
        render_marketing_analytics_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/export.csv")
def admin_marketing_analytics_export(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    date_range = parse_analytics_date_range(date_from=date_from, date_to=date_to)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        csv_body = render_analytics_export_csv(
            build_preview_marketing_analytics_data(date_range=date_range)
        )
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="marketing-analytics.csv"'},
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    from app.repositories.postgres import get_repositories

    with db.db_connection(settings.database_url) as conn:
        dashboard_data = load_marketing_analytics_dashboard(
            conn,
            get_repositories().marketing_analytics,
            date_range=date_range,
        )
    return Response(
        content=render_analytics_export_csv(dashboard_data),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="marketing-analytics.csv"'},
    )
