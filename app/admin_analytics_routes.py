"""Marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app import db
from app.admin_analytics_pages import render_marketing_analytics_page
from app.config import get_settings
from app.marketing_analytics_dashboard import (
    BriefFunnelCounts,
    MarketingAnalyticsDashboardData,
    dashboard_to_csv,
    load_marketing_analytics_dashboard,
    parse_analytics_date_range,
)
from app.repositories.postgres import get_repositories

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


def _resolve_date_range(
    *,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
):
    return parse_analytics_date_range(days=days, date_from=date_from, date_to=date_to)


def _empty_dashboard(date_range) -> MarketingAnalyticsDashboardData:
    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=(),
        server_events=(),
        brief_funnel=BriefFunnelCounts(leads=0, checkouts_opened=0, payments=0),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=datetime.now(timezone.utc),
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_analytics_dashboard(
    request: Request,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    date_range = _resolve_date_range(days=days, date_from=date_from, date_to=date_to)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        preview_data = build_preview_marketing_analytics_data(date_range=date_range)
        return HTMLResponse(
            render_marketing_analytics_page(
                data=preview_data,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    dashboard_data: MarketingAnalyticsDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
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
        dashboard_data = _empty_dashboard(date_range)

    return HTMLResponse(
        render_marketing_analytics_page(
            data=dashboard_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/export.csv", response_class=PlainTextResponse)
def admin_analytics_export_csv(
    request: Request,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> PlainTextResponse:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    date_range = _resolve_date_range(days=days, date_from=date_from, date_to=date_to)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        data = build_preview_marketing_analytics_data(date_range=date_range)
    elif settings.database_url:
        with db.db_connection(settings.database_url) as conn:
            data = load_marketing_analytics_dashboard(
                conn,
                get_repositories().marketing_analytics,
                date_range=date_range,
            )
    else:
        data = _empty_dashboard(date_range)

    return PlainTextResponse(
        dashboard_to_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="marketing-analytics.csv"'},
    )
