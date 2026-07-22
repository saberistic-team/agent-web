"""CSV export for aggregated marketing analytics dashboard results."""

from __future__ import annotations

import csv
import io

from app.analytics_dashboard import AnalyticsDashboardData, format_conversion_rate
from app.crm_export import neutralize_csv_cell


def render_analytics_dashboard_csv(data: AnalyticsDashboardData) -> str:
    """Render aggregated analytics dashboard metrics as CSV (no row-level events)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "metric", "value", "detail"])

    writer.writerow(["meta", "range_label", data.date_range.label, ""])
    writer.writerow(["meta", "range_start_utc", data.date_range.start.isoformat(), ""])
    writer.writerow(["meta", "range_end_utc", data.date_range.end.isoformat(), "exclusive"])
    writer.writerow(["meta", "generated_at_utc", data.generated_at.isoformat(), ""])

    for row in data.event_counts:
        writer.writerow(
            [
                "event_volume",
                row.event_name,
                row.count,
                f"{row.source};{row.category}",
            ]
        )

    writer.writerow(["crm", "leads", data.crm_counts.leads, data.metric_definitions["crm_leads"]])
    writer.writerow(
        ["crm", "checkouts", data.crm_counts.checkouts, data.metric_definitions["crm_checkouts"]]
    )
    writer.writerow(
        ["crm", "payments", data.crm_counts.payments, data.metric_definitions["crm_payments"]]
    )

    for rate in data.conversion_rates:
        pct = format_conversion_rate(rate.numerator, rate.denominator)
        writer.writerow(
            [
                "conversion_rate",
                rate.label,
                "" if pct is None else f"{pct}%",
                f"numerator={rate.numerator} ({rate.numerator_definition}); "
                f"denominator={rate.denominator} ({rate.denominator_definition})",
            ]
        )

    for bucket in data.attribution:
        writer.writerow(
            [
                "attribution",
                bucket.dimension,
                bucket.event_count,
                f"key={neutralize_csv_cell(bucket.key)}; leads={bucket.lead_count}",
            ]
        )

    for row in data.case_study_engagement:
        writer.writerow(["content", "case_study", row.views, neutralize_csv_cell(row.slug)])

    for row in data.article_engagement:
        writer.writerow(["content", "article", row.views, neutralize_csv_cell(row.slug)])

    return buffer.getvalue()
