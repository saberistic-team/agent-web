"""Marketing analytics admin routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import db
from app.admin_marketing_analytics_pages import render_marketing_analytics_page
from app.config import get_settings
from app.marketing_analytics import (
    MarketingAnalyticsData,
    load_marketing_analytics,
    parse_period_days,
    render_marketing_analytics_csv,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_marketing_analytics(request: Request) -> HTMLResponse:
    from app.admin_routes import (
        _session_csrf_for_forms,
        require_admin_session,
    )

    session = require_admin_session(request)
    settings = get_settings()
    csrf_token = _session_csrf_for_forms(request, settings)
    period_days = parse_period_days(request.query_params.get("period"))

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        try:
            preview_data = build_preview_marketing_analytics_data(period_days=period_days)
        except Exception:
            logger.exception("Failed to build preview marketing analytics")
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return HTMLResponse(
            render_marketing_analytics_page(
                data=preview_data,
                admin_username=session.admin_username,
                csrf_token=csrf_token,
                preview_banner="Preview data — not production",
            )
        )

    analytics_data: MarketingAnalyticsData | None = None
    db_error = False
    if settings.database_url:
        try:
            from app.repositories.postgres import get_repositories

            with db.db_connection(settings.database_url) as conn:
                analytics_data = load_marketing_analytics(
                    conn,
                    get_repositories().marketing_analytics,
                    period_days=period_days,
                )
        except Exception:
            logger.exception("Failed to load marketing analytics")
            db_error = True

    if analytics_data is None:
        reference = datetime.now(timezone.utc)
        analytics_data = MarketingAnalyticsData(
            period_days=period_days,
            period_start=reference - timedelta(days=period_days),
            period_end=reference,
            engagement_counts=(),
            server_conversion_counts=(),
            conversion_rates=(),
            attribution_rows=(),
            case_study_engagement=(),
            article_engagement=(),
            abandoned_checkouts=0,
            generated_at=reference,
        )

    return HTMLResponse(
        render_marketing_analytics_page(
            data=analytics_data,
            admin_username=session.admin_username,
            csrf_token=csrf_token,
            db_error=db_error,
        )
    )


@router.get("/export.csv")
def admin_marketing_analytics_export(request: Request) -> Response:
    from app.admin_routes import require_admin_session

    require_admin_session(request)
    settings = get_settings()
    period_days = parse_period_days(request.query_params.get("period"))

    if settings.admin_preview_enabled:
        from app.admin_preview import build_preview_marketing_analytics_data

        data = build_preview_marketing_analytics_data(period_days=period_days)
        return Response(
            content=render_marketing_analytics_csv(data),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="marketing-analytics.csv"'
            },
        )

    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database unavailable.")

    from app.repositories.postgres import get_repositories

    with db.db_connection(settings.database_url) as conn:
        data = load_marketing_analytics(
            conn,
            get_repositories().marketing_analytics,
            period_days=period_days,
        )
    return Response(
        content=render_marketing_analytics_csv(data),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="marketing-analytics.csv"'
        },
    )
