"""Aggregated analytics dashboard CSV export."""

from __future__ import annotations

import csv
import io

from app.analytics_dashboard import DASHBOARD_TIMEZONE, AnalyticsDashboardData
from app.crm_export import neutralize_csv_cell


def render_analytics_export_csv(data: AnalyticsDashboardData) -> str:
    """Render aggregated funnel, conversion, attribution, and content metrics as CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["section", "metric", "value", "source", "definition"])
    for row in data.event_volumes:
        writer.writerow(
            [
                "event_volume",
                neutralize_csv_cell(row.label),
                row.count,
                row.source,
                neutralize_csv_cell(data.metric_definitions["event_volume"]),
            ]
        )

    for row in data.conversion_rates:
        rate = "" if row.rate_pct is None else f"{row.rate_pct:.1f}%"
        writer.writerow(
            [
                "conversion_rate",
                neutralize_csv_cell(row.label),
                neutralize_csv_cell(f"{row.numerator}/{row.denominator} = {rate}"),
                "computed",
                neutralize_csv_cell(row.definition),
            ]
        )

    for row in data.attribution_rows:
        writer.writerow(
            [
                "attribution",
                neutralize_csv_cell(
                    f"{row.utm_source} / {row.utm_medium} / {row.utm_campaign}"
                ),
                neutralize_csv_cell(
                    f"landing={row.landing_views}; brief={row.brief_starts}; "
                    f"leads={row.leads}; checkout={row.checkouts}; paid={row.payments}"
                ),
                "aggregated",
                neutralize_csv_cell(data.metric_definitions["attribution"]),
            ]
        )

    for row in data.case_study_engagement:
        writer.writerow(
            [
                "content_engagement",
                neutralize_csv_cell(f"case_study:{row.slug}"),
                row.views,
                "browser",
                neutralize_csv_cell(data.metric_definitions["content_engagement"]),
            ]
        )

    for row in data.article_engagement:
        writer.writerow(
            [
                "content_engagement",
                neutralize_csv_cell(f"article:{row.slug}"),
                row.views,
                "browser",
                neutralize_csv_cell(data.metric_definitions["content_engagement"]),
            ]
        )

    writer.writerow(
        [
            "meta",
            "date_range",
            neutralize_csv_cell(f"{data.date_from.isoformat()}..{data.date_to.isoformat()}"),
            DASHBOARD_TIMEZONE,
            neutralize_csv_cell(
                f"Generated {data.generated_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
            ),
        ]
    )
    return buffer.getvalue()
