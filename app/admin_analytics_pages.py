"""HTML for the marketing analytics admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics_dashboard import (
    DASHBOARD_TIMEZONE,
    MAX_RANGE_DAYS,
    VALID_PRESET_DAYS,
    AttributionRow,
    ContentEngagementRow,
    ConversionRateRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    dashboard_has_data,
    utm_attribution_key_list,
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{rate:.1f}%"


def _range_query(data: MarketingAnalyticsDashboardData) -> str:
    params: dict[str, str] = {}
    if data.date_range.preset_days is not None:
        params["days"] = str(data.date_range.preset_days)
    else:
        if data.date_range.from_date:
            params["date_from"] = data.date_range.from_date.isoformat()
        if data.date_range.to_date:
            params["date_to"] = data.date_range.to_date.isoformat()
    return urlencode(params)


def _render_date_range_form(data: MarketingAnalyticsDashboardData) -> str:
    preset = data.date_range.preset_days
    from_value = data.date_range.from_date.isoformat() if data.date_range.from_date else ""
    to_value = data.date_range.to_date.isoformat() if data.date_range.to_date else ""
    preset_links = []
    for days in sorted(VALID_PRESET_DAYS):
        active = " is-active" if preset == days else ""
        preset_links.append(
            f'<a class="dashboard-range-link{active}" href="/admin/analytics?days={days}">'
            f"{days}d</a>"
        )
    export_qs = _range_query(data)
    export_href = f"/admin/analytics/export.csv?{export_qs}" if export_qs else "/admin/analytics/export.csv"
    return f"""<form class="dashboard-range-form" method="get" action="/admin/analytics">
      <fieldset class="dashboard-range-presets">
        <legend class="admin-note">Quick range ({DASHBOARD_TIMEZONE})</legend>
        {' · '.join(preset_links)}
      </fieldset>
      <div class="dashboard-range-custom">
        <label for="date_from">From</label>
        <input id="date_from" name="date_from" type="date" value="{_esc(from_value)}" />
        <label for="date_to">To</label>
        <input id="date_to" name="date_to" type="date" value="{_esc(to_value)}" />
        <button type="submit" class="dashboard-secondary-btn">Apply</button>
      </div>
      <p class="admin-note">Custom ranges are capped at {MAX_RANGE_DAYS} days. All timestamps use {DASHBOARD_TIMEZONE}.</p>
      <p class="dashboard-actions">
        <a class="dashboard-secondary-link" href="{_esc(export_href)}">Export CSV (aggregates only)</a>
      </p>
    </form>"""


def _render_event_rows(rows: tuple[EventCountRow, ...], *, empty_message: str) -> str:
    if not rows:
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.event_name)}</td>
          <td>{row.count}</td>
          <td><span class="dashboard-source-badge dashboard-source-{row.source}">{_esc(row.source)}</span></td>
        </tr>"""
        for row in rows
    )


def _render_conversion_rows(rows: tuple[ConversionRateRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No conversion data in this window.</td></tr>'
    body = []
    for row in rows:
        body.append(
            f"""<tr>
              <td>{_esc(row.name)}</td>
              <td>{row.numerator}</td>
              <td>{row.denominator}</td>
              <td>{_format_rate(row.rate_percent)}</td>
              <td class="dashboard-metric-detail">{_esc(row.numerator_definition)} ÷ {_esc(row.denominator_definition)}</td>
            </tr>"""
        )
    return "".join(body)


def _render_attribution_rows(rows: tuple[AttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="6" class="audit-empty">No attributed traffic in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.source)}</td>
          <td>{_esc(row.medium)}</td>
          <td>{_esc(row.campaign)}</td>
          <td>{row.engagement_events}</td>
          <td>{row.leads}</td>
          <td>{row.payments}</td>
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
    utm_keys = ", ".join(utm_attribution_key_list())
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if db_error:
        status_block = """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif not dashboard_has_data(data):
        status_block = """<p class="admin-note dashboard-empty-note">
            No events recorded in this window yet. Browser engagement and server conversions
            will appear here once first-party analytics is enabled in production.
          </p>"""
    else:
        status_block = ""

    window_start = _format_timestamp(data.date_range.start)
    window_end = _format_timestamp(data.date_range.end)

    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Funnel &amp; attribution</h1>
      <p class="admin-lede">
        First-party engagement and conversion metrics for {_esc(data.date_range.label)}.
        Window {_esc(window_start)} → {_esc(window_end)} ({DASHBOARD_TIMEZONE}).
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {_render_date_range_form(data)}
      {status_block}
      <section class="dashboard-panel" aria-labelledby="analytics-engagement-title">
        <h2 class="admin-section-title" id="analytics-engagement-title">Browser engagement</h2>
        <p class="dashboard-metric-def">{_esc(definitions["engagement_events"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
            <tbody>{_render_event_rows(data.engagement_events, empty_message="No engagement events.")}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-server-title">
        <h2 class="admin-section-title" id="analytics-server-title">Server conversions</h2>
        <p class="dashboard-metric-def">{_esc(definitions["server_events"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
            <tbody>{_render_event_rows(data.server_events, empty_message="No server events.")}</tbody>
          </table>
        </div>
        <p class="admin-note">
          CRM cross-check — leads: {data.brief_funnel.leads}, checkouts: {data.brief_funnel.checkouts_opened},
          payments: {data.brief_funnel.payments} ({_esc(definitions["brief_leads"])}).
        </p>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-conversion-title">
        <h2 class="admin-section-title" id="analytics-conversion-title">Conversion rates</h2>
        <p class="dashboard-metric-def">{_esc(definitions["conversion_rate"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Step</th><th>Num.</th><th>Denom.</th><th>Rate</th><th>Definition</th></tr></thead>
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">Attribution ({_esc(utm_keys)})</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Source</th><th>Medium</th><th>Campaign</th><th>Engagement</th><th>Leads</th><th>Payments</th></tr></thead>
            <tbody>{_render_attribution_rows(data.attribution)}</tbody>
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
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.article_engagement, empty_message="No insight views.")}</tbody>
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
