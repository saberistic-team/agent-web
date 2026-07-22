"""Marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.actor_context import actor_context_from_request
from app.config import get_settings
from app.crm_service import CrmService
from app.marketing_analytics_dashboard import load_marketing_analytics_dashboard
from app.marketing_analytics_export import render_marketing_analytics_export_csv
from app.marketing_analytics_pages import render_marketing_analytics_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])
_crm = CrmService()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_marketing_analytics(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    from app.admin_routes import _session_csrf_for_forms, require_admin_session

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        return HTMLResponse(
            render_marketing_analytics_page(
                data=build_preview_marketing_analytics_data(
                    date_from=date_from,
                    date_to=date_to,
                ),
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    from app.marketing_analytics_dashboard import MarketingAnalyticsDashboardData, normalize_filters

    dashboard_data: MarketingAnalyticsDashboardData | None = None
    db_error = False
    if settings.database_url:
        try:
            from app.repositories.postgres import get_repositories

            with db.db_connection(settings.database_url) as conn:
                dashboard_data = load_marketing_analytics_dashboard(
                    conn,
                    get_repositories().marketing_analytics,
                    date_from=date_from,
                    date_to=date_to,
                )
        except Exception:
            logger.exception("Failed to load marketing analytics dashboard")
            db_error = True

    if dashboard_data is None:
        filters = normalize_filters(date_from=date_from, date_to=date_to)
        dashboard_data = MarketingAnalyticsDashboardData(
            filters=filters,
            engagement_events=(),
            server_events=(),
            conversion_rates=(),
            attribution=(),
            case_study_views=(),
            article_views=(),
            generated_at=datetime.now(timezone.utc),
        )

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
    from app.repositories.postgres import get_repositories

    session = require_admin_session(request)
    settings = get_settings()
    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        data = build_preview_marketing_analytics_data(
            date_from=date_from,
            date_to=date_to,
        )
        return Response(
            content=render_marketing_analytics_export_csv(data),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="marketing-analytics.csv"'
            },
        )
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")

    actor = actor_context_from_request(request, actor=session.admin_username)
    with db.db_connection(settings.database_url) as conn:
        _crm.request_export(
            conn,
            actor_context=actor,
            export_type="marketing_analytics_csv",
            filters={
                "date_from": date_from,
                "date_to": date_to,
            },
        )
        data = load_marketing_analytics_dashboard(
            conn,
            get_repositories().marketing_analytics,
            date_from=date_from,
            date_to=date_to,
        )
    return Response(
        content=render_marketing_analytics_export_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="marketing-analytics.csv"'},
    )
