"""Admin HTML for the marketing analytics dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics_dashboard import (
    ALLOWED_PERIOD_DAYS,
    AnalyticsDashboardData,
    ConversionRateRow,
    EventCountRow,
)
from app.admin_layout import render_admin_shell


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _format_rate(row: ConversionRateRow) -> str:
    if row.rate_pct is None:
        return "—"
    return f"{row.rate_pct}%"


def _render_event_rows(rows: tuple[EventCountRow, ...], *, empty_message: str) -> str:
    if not rows:
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    body = []
    for row in rows:
        source_label = "Server (authoritative)" if row.source == "server" else "Browser"
        body.append(
            f"""<tr>
              <td>{_esc(row.event_name)}</td>
              <td>{row.count}</td>
              <td>{_esc(source_label)}</td>
            </tr>"""
        )
    return "".join(body)


def _render_conversion_rate_rows(rows: tuple[ConversionRateRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No conversion data in this window.</td></tr>'
    body = []
    for row in rows:
        body.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{_format_rate(row)}</td>
              <td>{row.numerator}</td>
              <td>{row.denominator}</td>
              <td class="analytics-rate-def">{_esc(row.numerator_definition)} ÷ {_esc(row.denominator_definition)}</td>
            </tr>"""
        )
    return "".join(body)


def _render_attribution_rows(data: AnalyticsDashboardData) -> str:
    if not data.attribution_rows:
        return '<tr><td colspan="5" class="audit-empty">No attributed events in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.source)}</td>
          <td>{_esc(row.medium)}</td>
          <td>{_esc(row.campaign)}</td>
          <td>{row.event_count}</td>
          <td>{row.lead_count}</td>
        </tr>"""
        for row in data.attribution_rows
    )


def _render_content_rows(
    rows: tuple[Any, ...],
    *,
    empty_message: str,
) -> str:
    if not rows:
        return f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    max_views = max(row.views for row in rows) or 1
    body = []
    for row in rows:
        width = max(4, int(100 * row.views / max_views))
        body.append(
            f"""<tr>
              <td>{_esc(row.slug)}</td>
              <td>
                <span class="analytics-bar" style="width:{width}%" aria-hidden="true"></span>
                {row.views}
              </td>
            </tr>"""
        )
    return "".join(body)


def _period_options(selected_period: str) -> str:
    options = []
    for days in sorted(ALLOWED_PERIOD_DAYS):
        value = f"{days}d"
        selected = ' selected="selected"' if value == selected_period else ""
        options.append(f'<option value="{value}"{selected}>Last {days} days</option>')
    return "".join(options)


def render_analytics_dashboard_page(
    *,
    data: AnalyticsDashboardData,
    admin_username: str,
    csrf_token: str = "",
    selected_period: str = "7d",
    db_error: bool = False,
    preview_banner: str | None = None,
    range_error: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    definitions = data.metric_definitions
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    error_html = ""
    if db_error:
        error_html = """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif range_error:
        error_html = f"""<p class="brief-error" role="alert">{_esc(range_error)}</p>"""

    export_href = f"/admin/analytics/export.csv?period={_esc(selected_period)}"
    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="analytics-title">
      {banner_html}
      {error_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Analytics</h1>
      <p class="admin-lede">
        First-party funnel, attribution, and content engagement for {_esc(data.date_range.label)}.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      <form class="analytics-range-form" method="get" action="/admin/analytics">
        <label for="analytics-period">Date range</label>
        <select id="analytics-period" name="period">{_period_options(selected_period)}</select>
        <button class="admin-action admin-action--secondary" type="submit">Apply</button>
        <a class="dashboard-secondary-link" href="{export_href}">Export CSV</a>
      </form>
      <section class="dashboard-panel" aria-labelledby="analytics-engagement-title">
        <h2 class="admin-section-title" id="analytics-engagement-title">Engagement events</h2>
        <p class="dashboard-metric-def">{_esc(definitions["engagement_events"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
            <tbody>{_render_event_rows(data.engagement_events, empty_message="No engagement events in this window.")}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-conversion-title">
        <h2 class="admin-section-title" id="analytics-conversion-title">Authoritative conversions</h2>
        <p class="dashboard-metric-def">{_esc(definitions["conversion_events"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
            <tbody>{_render_event_rows(data.conversion_events, empty_message="No conversion events in this window.")}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-rates-title">
        <h2 class="admin-section-title" id="analytics-rates-title">Conversion rates</h2>
        <p class="dashboard-metric-def">{_esc(definitions["conversion_rates"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th>Step</th>
                <th>Rate</th>
                <th>Numerator</th>
                <th>Denominator</th>
                <th>Definition</th>
              </tr>
            </thead>
            <tbody>{_render_conversion_rate_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">Attribution (UTM)</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Medium</th>
                <th>Campaign</th>
                <th>Events</th>
                <th>Leads</th>
              </tr>
            </thead>
            <tbody>{_render_attribution_rows(data)}</tbody>
          </table>
        </div>
      </section>
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-case-studies-title">
          <h2 class="admin-section-title" id="analytics-case-studies-title">Case study views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views in this window.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.article_engagement, empty_message="No insight views in this window.")}</tbody>
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
