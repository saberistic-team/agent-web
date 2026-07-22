"""First-party marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.admin_analytics_pages import render_analytics_dashboard_page
from app.analytics_dashboard import AnalyticsDashboardData, load_analytics_dashboard
from app.analytics_export import render_analytics_export_csv
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


def _empty_dashboard(*, date_from: str | None, date_to: str | None) -> AnalyticsDashboardData:
    from app.analytics_dashboard import parse_analytics_date_range

    _, _, start_day, end_day = parse_analytics_date_range(date_from, date_to)
    return AnalyticsDashboardData(
        date_from=start_day,
        date_to=end_day,
        event_volumes=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=datetime.now(timezone.utc),
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_analytics_dashboard(request: Request) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    date_from = request.query_params.get("from")
    date_to = request.query_params.get("to")

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        try:
            preview_data = build_preview_analytics_dashboard_data(
                date_from=date_from,
                date_to=date_to,
            )
        except Exception:
            logger.exception("Failed to build preview analytics dashboard")
            raise HTTPException(status_code=500, detail="Internal Server Error") from None
        return HTMLResponse(
            render_analytics_dashboard_page(
                data=preview_data,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    dashboard_data: AnalyticsDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_analytics_dashboard(
                    conn,
                    date_from=date_from,
                    date_to=date_to,
                )
        except Exception:
            logger.exception("Failed to load analytics dashboard")
            db_error = True

    if dashboard_data is None:
        dashboard_data = _empty_dashboard(date_from=date_from, date_to=date_to)

    return HTMLResponse(
        render_analytics_dashboard_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/export.csv")
def admin_analytics_export(request: Request) -> Response:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    date_from = request.query_params.get("from")
    date_to = request.query_params.get("to")

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        preview_data = build_preview_analytics_dashboard_data(
            date_from=date_from,
            date_to=date_to,
        )
        return Response(
            content=render_analytics_export_csv(preview_data),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    with db.db_connection(settings.database_url) as conn:
        dashboard_data = load_analytics_dashboard(
            conn,
            date_from=date_from,
            date_to=date_to,
        )
    return Response(
        content=render_analytics_export_csv(dashboard_data),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
    )
