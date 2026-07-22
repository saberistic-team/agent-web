"""CSV export for marketing analytics — aggregated metrics only."""

from __future__ import annotations

import csv
import io

from app.marketing_analytics_dashboard import MarketingAnalyticsDashboardData


def render_marketing_analytics_export_csv(data: MarketingAnalyticsDashboardData) -> str:
    """Render aggregated marketing analytics as CSV (no session-level rows)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "field", "value"])
    writer.writerow(["meta", "start_date", data.filters.start_raw])
    writer.writerow(["meta", "end_date", data.filters.end_raw])
    writer.writerow(["meta", "timezone", "UTC"])

    writer.writerow([])
    writer.writerow(["engagement_events", "event_name", "source", "count"])
    for row in data.engagement_events:
        writer.writerow(["engagement_events", row.event_name, row.source, row.count])

    writer.writerow([])
    writer.writerow(["server_events", "event_name", "source", "count"])
    for row in data.server_events:
        writer.writerow(["server_events", row.event_name, row.source, row.count])

    writer.writerow([])
    writer.writerow(
        [
            "conversion_rates",
            "label",
            "numerator",
            "denominator",
            "rate_pct",
            "numerator_definition",
            "denominator_definition",
        ]
    )
    for row in data.conversion_rates:
        writer.writerow(
            [
                "conversion_rates",
                row.label,
                row.numerator,
                row.denominator,
                "" if row.rate_pct is None else row.rate_pct,
                row.numerator_definition,
                row.denominator_definition,
            ]
        )

    writer.writerow([])
    writer.writerow(
        [
            "attribution",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "engagement_events",
            "leads",
            "payments",
        ]
    )
    for row in data.attribution:
        writer.writerow(
            [
                "attribution",
                row.utm_source,
                row.utm_medium,
                row.utm_campaign,
                row.engagement_events,
                row.leads,
                row.payments,
            ]
        )

    writer.writerow([])
    writer.writerow(["case_study_views", "slug", "views"])
    for row in data.case_study_views:
        writer.writerow(["case_study_views", row.slug, row.views])

    writer.writerow([])
    writer.writerow(["article_views", "slug", "views"])
    for row in data.article_views:
        writer.writerow(["article_views", row.slug, row.views])

    return buffer.getvalue()
