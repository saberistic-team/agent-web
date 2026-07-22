"""Admin HTML for the marketing analytics dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics_dashboard import (
    DASHBOARD_TIMEZONE,
    ConversionRateRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
)


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


def _render_date_filter(data: MarketingAnalyticsDashboardData) -> str:
    export_query = (
        f"?date_from={_esc(data.filters.start_raw)}&date_to={_esc(data.filters.end_raw)}"
    )
    return f"""<form class="admin-filter-form dashboard-filter-form" method="get" action="/admin/analytics">
        <fieldset class="admin-filter-fieldset">
          <legend class="admin-filter-legend">Date range ({DASHBOARD_TIMEZONE})</legend>
          <label class="admin-filter-label">
            From
            <input type="date" name="date_from" value="{_esc(data.filters.start_raw)}" required>
          </label>
          <label class="admin-filter-label">
            To
            <input type="date" name="date_to" value="{_esc(data.filters.end_raw)}" required>
          </label>
          <button type="submit" class="dashboard-secondary-link">Apply</button>
          <a class="dashboard-secondary-link" href="/admin/analytics/export.csv{export_query}">Export CSV</a>
        </fieldset>
      </form>"""


def _render_event_rows(
    rows: tuple[EventCountRow, ...],
    *,
    empty_message: str,
) -> str:
    if not rows:
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.event_name)}</td>
          <td>{_esc(row.source)}</td>
          <td>{row.count}</td>
        </tr>"""
        for row in rows
    )


def _render_conversion_rows(rows: tuple[ConversionRateRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No conversion data in this window.</td></tr>'
    body: list[str] = []
    for row in rows:
        body.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{row.numerator}</td>
              <td>{row.denominator}</td>
              <td>{_format_rate(row.rate_pct)}</td>
              <td class="dashboard-metric-def">
                {_esc(row.numerator_definition)} ÷ {_esc(row.denominator_definition)}
              </td>
            </tr>"""
        )
    return "".join(body)


def _render_attribution_rows(data: MarketingAnalyticsDashboardData) -> str:
    if not data.attribution:
        return '<tr><td colspan="6" class="audit-empty">No attribution rows in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.utm_source)}</td>
          <td>{_esc(row.utm_medium)}</td>
          <td>{_esc(row.utm_campaign)}</td>
          <td>{row.engagement_events}</td>
          <td>{row.leads}</td>
          <td>{row.payments}</td>
        </tr>"""
        for row in data.attribution
    )


def _render_content_rows(
    rows: tuple[Any, ...],
    *,
    empty_message: str,
) -> str:
    if not rows:
        return f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"<tr><td>{_esc(row.slug)}</td><td>{row.views}</td></tr>" for row in rows
    )


def render_marketing_analytics_page(
    *,
    data: MarketingAnalyticsDashboardData,
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
        error_block = """<p class="brief-error" role="alert">
            Marketing analytics are temporarily unavailable. Try again shortly.
          </p>"""
    else:
        error_block = ""

    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Funnel &amp; attribution</h1>
      <p class="admin-lede">
        First-party engagement and conversion metrics for saberistic.com.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      <p class="dashboard-metric-def">{_esc(definitions["event_window"])}</p>
      {error_block}
      {_render_date_filter(data)}
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-engagement-title">
          <h2 class="admin-section-title" id="analytics-engagement-title">Browser engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["browser_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Source</th><th>Count</th></tr></thead>
              <tbody>{_render_event_rows(data.engagement_events, empty_message="No engagement events in this window.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-server-title">
          <h2 class="admin-section-title" id="analytics-server-title">Server conversions</h2>
          <p class="dashboard-metric-def">{_esc(definitions["server_conversion"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Source</th><th>Count</th></tr></thead>
              <tbody>{_render_event_rows(data.server_events, empty_message="No server conversion events in this window.")}</tbody>
            </table>
          </div>
        </section>
      </div>
      <section class="dashboard-panel" aria-labelledby="analytics-conversion-title">
        <h2 class="admin-section-title" id="analytics-conversion-title">Conversion rates</h2>
        <p class="dashboard-metric-def">Rates use explicit numerators and denominators; zero denominators show as em dash.</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th>Step</th>
                <th>Numerator</th>
                <th>Denominator</th>
                <th>Rate</th>
                <th>Definition</th>
              </tr>
            </thead>
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">UTM attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Medium</th>
                <th>Campaign</th>
                <th>Engagement</th>
                <th>Leads</th>
                <th>Payments</th>
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
              <tbody>{_render_content_rows(data.case_study_views, empty_message="No case study views in this window.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.article_views, empty_message="No insight views in this window.")}</tbody>
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
