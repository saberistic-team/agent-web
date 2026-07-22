"""Marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.analytics_dashboard import (
    AnalyticsDashboardData,
    AnalyticsDateRange,
    load_analytics_dashboard,
    parse_analytics_date_range,
    render_analytics_export_csv,
)
from app.config import get_settings
from app.admin_analytics_pages import render_analytics_dashboard_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


def _selected_period(request: Request) -> str:
    period = (request.query_params.get("period") or "7d").strip()
    return period or "7d"


def _resolve_date_range(request: Request) -> tuple[AnalyticsDateRange, str | None]:
    period = request.query_params.get("period")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    try:
        return parse_analytics_date_range(period=period, start=start, end=end), None
    except ValueError as exc:
        fallback = parse_analytics_date_range(period="7d")
        return fallback, str(exc)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_analytics_dashboard(request: Request) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    selected_period = _selected_period(request)
    date_range, range_error = _resolve_date_range(request)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        return HTMLResponse(
            render_analytics_dashboard_page(
                data=build_preview_analytics_dashboard_data(
                    period=selected_period,
                ),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                selected_period=selected_period,
                preview_banner="Preview data — not production",
            )
        )

    dashboard_data: AnalyticsDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
            from app.repositories.postgres import get_repositories

            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_analytics_dashboard(
                    conn,
                    get_repositories().analytics_dashboard,
                    date_range=date_range,
                )
        except Exception:
            logger.exception("Failed to load analytics dashboard")
            db_error = True

    if dashboard_data is None:
        dashboard_data = AnalyticsDashboardData(
            date_range=date_range,
            engagement_events=(),
            conversion_events=(),
            conversion_rates=(),
            attribution_rows=(),
            case_study_engagement=(),
            article_engagement=(),
            generated_at=datetime.now(timezone.utc),
        )

    return HTMLResponse(
        render_analytics_dashboard_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            selected_period=selected_period,
            db_error=db_error,
            range_error=range_error,
        )
    )


@router.get("/export.csv")
def admin_analytics_export(request: Request) -> Response:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    selected_period = _selected_period(request)
    date_range, _ = _resolve_date_range(request)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        data = build_preview_analytics_dashboard_data(period=selected_period)
        return Response(
            content=render_analytics_export_csv(data),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    from app.repositories.postgres import get_repositories

    with db.db_connection(settings.database_url) as conn:
        data = load_analytics_dashboard(
            conn,
            get_repositories().analytics_dashboard,
            date_range=date_range,
        )
    return Response(
        content=render_analytics_export_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
    )
