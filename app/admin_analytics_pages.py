"""Admin HTML for the marketing analytics dashboard."""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.analytics_dashboard import (
    ALLOWED_RANGE_PRESETS,
    AnalyticsDashboardData,
    AttributionBucket,
    ContentEngagementRow,
    ConversionRate,
    FunnelEventCount,
    format_conversion_rate,
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _render_range_form(*, selected_days: int | None, date_range_label: str) -> str:
    preset_links = []
    for days in ALLOWED_RANGE_PRESETS:
        selected = " aria-current=\"true\"" if selected_days == days else ""
        preset_links.append(
            f'<a class="analytics-range-link"{selected} href="/admin/analytics?days={days}">'
            f"{days}d</a>"
        )
    return f"""<form class="analytics-range-form" method="get" action="/admin/analytics">
      <fieldset class="analytics-range-presets">
        <legend class="admin-note">Quick range</legend>
        {" · ".join(preset_links)}
      </fieldset>
      <div class="analytics-range-custom">
        <label>Start <input type="date" name="start" /></label>
        <label>End <input type="date" name="end" /></label>
        <button class="admin-action admin-action--secondary" type="submit">Apply</button>
      </div>
      <p class="admin-note">Showing {_esc(date_range_label)} (UTC).</p>
    </form>"""


def _render_event_rows(rows: tuple[FunnelEventCount, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No events in range.</td></tr>'
    body = []
    for row in rows:
        source_label = "Browser" if row.source == "browser" else "Server"
        category_label = row.category.title()
        body.append(
            f"""<tr>
              <td>{_esc(row.event_name)}</td>
              <td>{row.count}</td>
              <td><span class="analytics-source analytics-source--{row.source}">{source_label}</span></td>
              <td>{category_label}</td>
            </tr>"""
        )
    return "".join(body)


def _render_conversion_rows(rows: tuple[ConversionRate, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No conversion rates.</td></tr>'
    body = []
    for rate in rows:
        pct = format_conversion_rate(rate.numerator, rate.denominator)
        rate_cell = "—" if pct is None else f"{pct}%"
        body.append(
            f"""<tr>
              <td>{_esc(rate.label)}</td>
              <td>{rate_cell}</td>
              <td>{rate.numerator}</td>
              <td>{rate.denominator}</td>
              <td class="analytics-rate-def">
                {_esc(rate.numerator_definition)} ÷ {_esc(rate.denominator_definition)}
                <span class="analytics-source analytics-source--{rate.source}">{_esc(rate.source)}</span>
              </td>
            </tr>"""
        )
    return "".join(body)


def _render_attribution_rows(rows: tuple[AttributionBucket, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No attribution in range.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(bucket.dimension)}</td>
          <td>{_esc(bucket.key)}</td>
          <td>{bucket.event_count}</td>
          <td>{bucket.lead_count}</td>
        </tr>"""
        for bucket in rows
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
    if range_error:
        error_html = f'<p class="brief-error" role="alert">{_esc(range_error)}</p>'
    if db_error:
        error_html += """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""

    export_query = ""
    if data.date_range.preset_days is not None:
        export_query = f"?days={data.date_range.preset_days}"
    else:
        start_day = data.date_range.start.date().isoformat()
        end_day = (data.date_range.end.date() - timedelta(days=1)).isoformat()
        export_query = f"?start={start_day}&end={end_day}"

    main = f"""<section class="admin-panel dashboard-root analytics-dashboard" aria-labelledby="analytics-title">
      {banner_html}
      {error_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Marketing analytics</h1>
      <p class="admin-lede">
        First-party engagement and conversion metrics for saberistic.com.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {_render_range_form(selected_days=data.date_range.preset_days, date_range_label=data.date_range.label)}
      <p class="dashboard-actions">
        <a class="dashboard-secondary-link" href="/admin/analytics/export.csv{export_query}">Download CSV</a>
      </p>
      <section class="dashboard-panel" aria-labelledby="analytics-events-title">
        <h2 class="admin-section-title" id="analytics-events-title">Event volume</h2>
        <p class="dashboard-metric-def">{_esc(definitions["event_volume"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th><th>Category</th></tr></thead>
            <tbody>{_render_event_rows(data.event_counts)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-crm-title">
        <h2 class="admin-section-title" id="analytics-crm-title">Authoritative CRM funnel</h2>
        <p class="dashboard-metric-def">{_esc(definitions["crm_leads"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table dashboard-count-table">
            <thead><tr><th>Metric</th><th>Count</th><th>Definition</th></tr></thead>
            <tbody>
              <tr><td>Leads persisted</td><td>{data.crm_counts.leads}</td><td>{_esc(definitions["crm_leads"])}</td></tr>
              <tr><td>Checkouts opened</td><td>{data.crm_counts.checkouts}</td><td>{_esc(definitions["crm_checkouts"])}</td></tr>
              <tr><td>Paid diagnostics</td><td>{data.crm_counts.payments}</td><td>{_esc(definitions["crm_payments"])}</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-rates-title">
        <h2 class="admin-section-title" id="analytics-rates-title">Conversion rates</h2>
        <p class="dashboard-metric-def">Rates show numerator ÷ denominator with explicit definitions. Zero denominators render as em dash.</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Step</th><th>Rate</th><th>Num</th><th>Den</th><th>Definition</th></tr></thead>
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">Attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Dimension</th><th>Value</th><th>Events</th><th>Leads</th></tr></thead>
            <tbody>{_render_attribution_rows(data.attribution)}</tbody>
          </table>
        </div>
      </section>
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-case-studies-title">
          <h2 class="admin-section-title" id="analytics-case-studies-title">Case study engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table dashboard-count-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views in range.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table dashboard-count-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.article_engagement, empty_message="No insight views in range.")}</tbody>
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
