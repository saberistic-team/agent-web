"""HTML and CSV for the marketing analytics admin dashboard."""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics_dashboard import (
    AnalyticsDashboardData,
    AttributionRow,
    ContentEngagementRow,
    ConversionRate,
    EventCount,
    dashboard_has_data,
)
from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _format_rate(rate_pct: float | None) -> str:
    if rate_pct is None:
        return "—"
    return f"{rate_pct:.1f}%"


def _source_badge(source: str) -> str:
    label = "Server" if source == "server" else "Browser"
    css = "analytics-source-server" if source == "server" else "analytics-source-browser"
    return f'<span class="analytics-source-badge {css}">{label}</span>'


def _render_date_filter(*, data: AnalyticsDashboardData) -> str:
    query = f"from={_esc(data.date_range.from_raw)}&amp;to={_esc(data.date_range.to_raw)}"
    return f"""<form class="analytics-date-filter" method="get" action="/admin/analytics">
      <label class="analytics-date-label">
        From
        <input type="date" name="from" value="{_esc(data.date_range.from_raw)}" required />
      </label>
      <label class="analytics-date-label">
        To
        <input type="date" name="to" value="{_esc(data.date_range.to_raw)}" required />
      </label>
      <button type="submit" class="dashboard-secondary-link">Apply range</button>
      <a class="dashboard-secondary-link" href="/admin/analytics/export.csv?{query}">Export CSV</a>
    </form>"""


def _render_event_rows(rows: tuple[EventCount, ...], *, empty_message: str) -> str:
    if not rows or not any(row.count for row in rows):
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.label)}</td>
          <td>{row.count}</td>
          <td>{_source_badge(row.source)}</td>
        </tr>"""
        for row in rows
    )


def _render_conversion_rows(rows: tuple[ConversionRate, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No conversion data.</td></tr>'
    body = []
    for row in rows:
        body.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{row.numerator} <span class="analytics-rate-detail">({_esc(row.numerator_label)}, {_source_badge(row.numerator_source)})</span></td>
              <td>{row.denominator} <span class="analytics-rate-detail">({_esc(row.denominator_label)}, {_source_badge(row.denominator_source)})</span></td>
              <td>{_format_rate(row.rate_pct)}</td>
            </tr>"""
        )
    return "".join(body)


def _render_attribution_rows(rows: tuple[AttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No attributed events in this range.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.source)}</td>
          <td>{_esc(row.medium)}</td>
          <td>{_esc(row.campaign)}</td>
          <td>{row.total_events}</td>
          <td>{row.leads}</td>
        </tr>"""
        for row in rows
    )


def _render_content_rows(
    rows: tuple[ContentEngagementRow, ...],
    *,
    empty_message: str,
) -> str:
    if not rows:
        return f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"<tr><td>{_esc(row.slug)}</td><td>{row.views}</td></tr>" for row in rows
    )


def render_analytics_dashboard_page(
    *,
    data: AnalyticsDashboardData,
    admin_username: str,
    csrf_token: str = "",
    db_error: bool = False,
    preview_banner: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    definitions = data.metric_definitions
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if db_error:
        empty_block = """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif not dashboard_has_data(data):
        empty_block = """<section class="dashboard-empty" aria-labelledby="analytics-empty-title">
          <h2 class="admin-section-title" id="analytics-empty-title">No events yet</h2>
          <p class="admin-lede">
            First-party analytics events will appear here once site traffic is recorded
            for the selected date range.
          </p>
        </section>"""
    else:
        empty_block = ""

    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Analytics</h1>
      <p class="admin-lede">
        Funnel, attribution, and content engagement for
        <time datetime="{_esc(data.date_range.from_raw)}">{_esc(data.date_range.from_raw)}</time>
        through
        <time datetime="{_esc(data.date_range.to_raw)}">{_esc(data.date_range.to_raw)}</time>
        (UTC). Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {_render_date_filter(data=data)}
      {empty_block}
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-engagement-title">
          <h2 class="admin-section-title" id="analytics-engagement-title">Browser engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["engagement_events"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
              <tbody>{_render_event_rows(data.engagement_counts, empty_message="No engagement events.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-server-title">
          <h2 class="admin-section-title" id="analytics-server-title">Server conversions</h2>
          <p class="dashboard-metric-def">{_esc(definitions["server_conversions"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
              <tbody>{_render_event_rows(data.server_counts, empty_message="No server conversion events.")}</tbody>
            </table>
          </div>
        </section>
      </div>
      <section class="dashboard-panel" aria-labelledby="analytics-rates-title">
        <h2 class="admin-section-title" id="analytics-rates-title">Conversion rates</h2>
        <p class="dashboard-metric-def">{_esc(definitions["conversion_rate"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Step</th><th>Numerator</th><th>Denominator</th><th>Rate</th></tr></thead>
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">Attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Source</th><th>Medium</th><th>Campaign</th><th>Events</th><th>Leads</th></tr></thead>
            <tbody>{_render_attribution_rows(data.attribution)}</tbody>
          </table>
        </div>
      </section>
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-case-studies-title">
          <h2 class="admin-section-title" id="analytics-case-studies-title">Case study engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.case_studies, empty_message="No case study views.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Article engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.articles, empty_message="No article views.")}</tbody>
            </table>
          </div>
        </section>
      </div>
    </section>"""
    return render_admin_shell(
        title="Analytics",
        main=main,
        active_path="/admin/analytics",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )


def render_analytics_dashboard_csv(data: AnalyticsDashboardData) -> str:
    """Return aggregated analytics as CSV (no row-level visitor data)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "metric", "value", "detail"])
    writer.writerow(["meta", "from_date", data.date_range.from_raw, ""])
    writer.writerow(["meta", "to_date", data.date_range.to_raw, ""])
    writer.writerow(["meta", "generated_at", data.generated_at.isoformat(), ""])

    for row in data.engagement_counts:
        writer.writerow(["engagement", row.label, row.count, row.source])
    for row in data.server_counts:
        writer.writerow(["server_conversion", row.label, row.count, row.source])

    for row in data.conversion_rates:
        writer.writerow(
            [
                "conversion_rate",
                row.label,
                _format_rate(row.rate_pct),
                f"{row.numerator}/{row.denominator} ({row.numerator_label}/{row.denominator_label})",
            ]
        )

    for row in data.attribution:
        writer.writerow(
            [
                "attribution",
                row.source,
                row.total_events,
                f"medium={row.medium}; campaign={row.campaign}; leads={row.leads}",
            ]
        )

    for row in data.case_studies:
        writer.writerow(["case_study", row.slug, row.views, ""])
    for row in data.articles:
        writer.writerow(["article", row.slug, row.views, ""])

    return buffer.getvalue()
