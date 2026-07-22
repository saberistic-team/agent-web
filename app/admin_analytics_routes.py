"""Marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.actor_context import actor_context_from_request
from app.analytics_dashboard import (
    AnalyticsDashboardData,
    AnalyticsDateRange,
    CrmFunnelCounts,
    load_analytics_dashboard,
    parse_analytics_date_range,
)
from app.analytics_export import render_analytics_dashboard_csv
from app.admin_analytics_pages import render_analytics_dashboard_page
from app.config import get_settings
from app.crm_service import CrmService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])
_crm = CrmService()


def _parse_range_from_request(request: Request) -> tuple[AnalyticsDateRange, str | None]:
    params = request.query_params
    try:
        return parse_analytics_date_range(
            days=params.get("days"),
            start=params.get("start"),
            end=params.get("end"),
        ), None
    except ValueError as exc:
        date_range = parse_analytics_date_range()
        return date_range, str(exc)


def _empty_dashboard(date_range: AnalyticsDateRange) -> AnalyticsDashboardData:
    return AnalyticsDashboardData(
        date_range=date_range,
        event_counts=(),
        crm_counts=CrmFunnelCounts(leads=0, checkouts=0, payments=0),
        conversion_rates=(),
        attribution=(),
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
    date_range, range_error = _parse_range_from_request(request)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        return HTMLResponse(
            render_analytics_dashboard_page(
                data=build_preview_analytics_dashboard_data(date_range=date_range),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
                range_error=range_error,
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
        dashboard_data = _empty_dashboard(date_range)

    return HTMLResponse(
        render_analytics_dashboard_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
            range_error=range_error,
        )
    )


@router.get("/export.csv")
def admin_analytics_export(request: Request) -> Response:
    from app.admin_routes import require_admin_session
    from app.repositories.postgres import get_repositories

    session = require_admin_session(request)
    settings = get_settings()
    date_range, _range_error = _parse_range_from_request(request)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_analytics_dashboard_data

        csv_text = render_analytics_dashboard_csv(
            build_preview_analytics_dashboard_data(date_range=date_range)
        )
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.request_export(
            conn,
            actor_context=actor,
            export_type="analytics_dashboard_csv",
            filters={
                "start": date_range.start.isoformat(),
                "end": date_range.end.isoformat(),
                "preset_days": date_range.preset_days,
            },
        )
        csv_text = render_analytics_dashboard_csv(
            load_analytics_dashboard(
                conn,
                get_repositories().analytics_dashboard,
                date_range=date_range,
            )
        )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
    )
