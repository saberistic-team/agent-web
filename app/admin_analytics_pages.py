"""HTML for the marketing analytics admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics_dashboard import (
    DASHBOARD_TIMEZONE,
    MarketingAnalyticsDashboardData,
    dashboard_has_data,
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


def _date_filter_form(data: MarketingAnalyticsDashboardData) -> str:
    dr = data.date_range
    export_qs = f"date_from={_esc(dr.date_from_raw)}&date_to={_esc(dr.date_to_raw)}"
    return f"""<form class="analytics-filter-form" method="get" action="/admin/analytics">
      <fieldset class="analytics-filter-fieldset">
        <legend class="admin-eyebrow">Date range ({_esc(DASHBOARD_TIMEZONE)})</legend>
        <label>From
          <input type="date" name="date_from" value="{_esc(dr.date_from_raw)}" required />
        </label>
        <label>To
          <input type="date" name="date_to" value="{_esc(dr.date_to_raw)}" required />
        </label>
        <button class="admin-action" type="submit">Apply</button>
        <a class="admin-action admin-action--secondary" href="/admin/analytics/export.csv?{export_qs}">Export CSV</a>
      </fieldset>
    </form>"""


def _render_event_counts(data: MarketingAnalyticsDashboardData) -> str:
    definitions = data.metric_definitions
    rows = []
    for row in data.event_counts:
        source_label = "Server" if row.authoritative else "Browser"
        source_class = "analytics-source-server" if row.authoritative else "analytics-source-browser"
        rows.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td><code>{_esc(row.event_name)}</code></td>
              <td><span class="{source_class}">{source_label}</span></td>
              <td>{row.count}</td>
            </tr>"""
        )
    body = "".join(rows) if rows else (
        '<tr><td colspan="4" class="audit-empty">No events in this range.</td></tr>'
    )
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-events-title">
      <h2 class="admin-section-title" id="analytics-events-title">Funnel &amp; engagement events</h2>
      <p class="dashboard-metric-def">{_esc(definitions["event_counts"])}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col">Event</th>
              <th scope="col">Source</th>
              <th scope="col">Count</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>"""


def _render_conversion_rates(data: MarketingAnalyticsDashboardData) -> str:
    definitions = data.metric_definitions
    rows = []
    for row in data.conversion_rates:
        definition = (
            f"{row.numerator_event} ({row.numerator}) ÷ "
            f"{row.denominator_event} ({row.denominator})"
        )
        rows.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{row.numerator}</td>
              <td>{row.denominator}</td>
              <td>{_format_rate(row.rate_pct)}</td>
              <td class="analytics-rate-def">{_esc(definition)}</td>
            </tr>"""
        )
    body = "".join(rows)
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-rates-title">
      <h2 class="admin-section-title" id="analytics-rates-title">Conversion rates</h2>
      <p class="dashboard-metric-def">{_esc(definitions["conversion_rates"])}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Numerator</th>
              <th scope="col">Denominator</th>
              <th scope="col">Rate</th>
              <th scope="col">Definition</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>"""


def _render_attribution(data: MarketingAnalyticsDashboardData) -> str:
    definitions = data.metric_definitions
    if data.attribution:
        rows = "".join(
            f"""<tr>
              <td>{_esc(row.utm_source)}</td>
              <td>{_esc(row.utm_medium)}</td>
              <td>{_esc(row.utm_campaign)}</td>
              <td>{row.event_count}</td>
            </tr>"""
            for row in data.attribution
        )
    else:
        rows = '<tr><td colspan="4" class="audit-empty">No attributed events in this range.</td></tr>'
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
      <h2 class="admin-section-title" id="analytics-attribution-title">UTM attribution</h2>
      <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Medium</th>
              <th scope="col">Campaign</th>
              <th scope="col">Events</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""


def _render_content_table(
    *,
    title: str,
    section_id: str,
    definition: str,
    rows: tuple[Any, ...],
    empty_message: str,
) -> str:
    if rows:
        body = "".join(
            f"<tr><td>{_esc(row.slug)}</td><td>{row.view_count}</td></tr>" for row in rows
        )
    else:
        body = f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return f"""<section class="dashboard-panel" aria-labelledby="{section_id}">
      <h2 class="admin-section-title" id="{section_id}">{_esc(title)}</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr><th scope="col">Slug</th><th scope="col">Views</th></tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>"""


def render_marketing_analytics_page(
    *,
    data: MarketingAnalyticsDashboardData,
    admin_username: str,
    csrf_token: str = "",
    db_error: bool = False,
    preview_banner: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    dr = data.date_range
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    if db_error:
        error_block = """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif not dashboard_has_data(data):
        error_block = """<p class="admin-note">No analytics events recorded for this date range yet.</p>"""
    else:
        error_block = ""

    main = f"""{banner_html}
        <section class="dashboard-header" aria-labelledby="analytics-title">
          <p class="admin-eyebrow">Marketing analytics</p>
          <h1 class="admin-title" id="analytics-title">Funnel &amp; attribution</h1>
          <p class="admin-lede">
            First-party events from {_esc(dr.date_from_raw)} through {_esc(dr.date_to_raw)}
            ({_esc(DASHBOARD_TIMEZONE)}). Generated {_esc(generated)}.
          </p>
        </section>
        {_date_filter_form(data)}
        {error_block}
        {_render_event_counts(data)}
        {_render_conversion_rates(data)}
        {_render_attribution(data)}
        {_render_content_table(
            title="Case study engagement",
            section_id="analytics-case-studies-title",
            definition=data.metric_definitions["case_study_engagement"],
            rows=data.case_study_engagement,
            empty_message="No case study views in this range.",
        )}
        {_render_content_table(
            title="Insight engagement",
            section_id="analytics-insights-title",
            definition=data.metric_definitions["insight_engagement"],
            rows=data.insight_engagement,
            empty_message="No insight views in this range.",
        )}"""

    return render_admin_shell(
        title="Analytics",
        main=main,
        active_path="/admin/analytics",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
