"""HTML for the marketing analytics admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics_dashboard import (
    ContentEngagementRow,
    ConversionRateRow,
    EventAttributionRow,
    EventCountRow,
    LeadAttributionRow,
    MarketingAnalyticsDashboardData,
    dashboard_is_empty,
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
    return f"{rate_pct}%"


def _render_date_filter(data: MarketingAnalyticsDashboardData) -> str:
    date_from = _esc(data.date_range.date_from_raw)
    date_to = _esc(data.date_range.date_to_raw)
    export_href = (
        f"/admin/analytics/export.csv?date_from={date_from}&date_to={date_to}"
    )
    return f"""<form class="dashboard-filter" method="get" action="/admin/analytics">
      <fieldset class="dashboard-filter-fieldset">
        <legend class="admin-note">Date range (UTC)</legend>
        <label>
          From
          <input type="date" name="date_from" value="{date_from}" />
        </label>
        <label>
          To
          <input type="date" name="date_to" value="{date_to}" />
        </label>
        <button type="submit" class="cta">Apply</button>
        <a class="dashboard-secondary-link" href="{export_href}">Export CSV</a>
      </fieldset>
    </form>"""


def _render_event_rows(
    rows: tuple[EventCountRow, ...],
    *,
    empty_message: str,
) -> str:
    if not rows or all(row.count == 0 for row in rows):
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    body = []
    for row in rows:
        if row.count == 0:
            continue
        source_label = {
            "browser": "Browser",
            "server": "Server",
            "client_supplementary": "Browser (UX)",
        }.get(row.source, row.source)
        body.append(
            f"""<tr>
              <td>{_esc(row.event_name)}</td>
              <td>{row.count}</td>
              <td>{_esc(source_label)}</td>
            </tr>"""
        )
    if not body:
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(body)


def _render_event_table(
    *,
    title: str,
    rows: tuple[EventCountRow, ...],
    definition: str,
    section_id: str,
    empty_message: str,
) -> str:
    return f"""<section class="dashboard-panel" aria-labelledby="{section_id}">
      <h2 class="admin-section-title" id="{section_id}">{_esc(title)}</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table dashboard-count-table">
          <thead><tr><th scope="col">Event</th><th scope="col">Count</th><th scope="col">Source</th></tr></thead>
          <tbody>{_render_event_rows(rows, empty_message=empty_message)}</tbody>
        </table>
      </div>
    </section>"""


def _render_conversion_rows(rows: tuple[ConversionRateRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No conversion data in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.label)}</td>
          <td>{row.numerator}</td>
          <td>{row.denominator}</td>
          <td>{_format_rate(row.rate_pct)}</td>
          <td class="dashboard-metric-def">{_esc(row.definition)}</td>
        </tr>"""
        for row in rows
    )


def _render_event_attribution_rows(rows: tuple[EventAttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No attributed events in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.utm_source)}</td>
          <td>{_esc(row.utm_medium)}</td>
          <td>{_esc(row.utm_campaign)}</td>
          <td>{row.event_count}</td>
        </tr>"""
        for row in rows
    )


def _render_lead_attribution_rows(rows: tuple[LeadAttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="audit-empty">No leads in this window.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.utm_source)}</td>
          <td>{_esc(row.utm_medium)}</td>
          <td>{_esc(row.utm_campaign)}</td>
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
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )
    range_label = (
        f"{_esc(data.date_range.date_from_raw)} – {_esc(data.date_range.date_to_raw)} UTC"
    )

    if db_error:
        empty_block = """<p class="brief-error" role="alert">
            Marketing analytics are temporarily unavailable. Try again shortly.
          </p>"""
    elif dashboard_is_empty(data):
        empty_block = """<section class="dashboard-empty" aria-labelledby="analytics-empty-title">
          <h2 class="admin-section-title" id="analytics-empty-title">No events yet</h2>
          <p class="admin-lede">
            First-party analytics will appear here once site traffic and brief submissions
            are recorded. Adjust the date range or enable analytics in production.
          </p>
        </section>"""
    else:
        empty_block = ""

    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Marketing analytics</h1>
      <p class="admin-lede">
        Aggregated first-party metrics for {_esc(range_label)}.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {_render_date_filter(data)}
      {empty_block}
      <div class="dashboard-grid">
        {_render_event_table(
            title="Page & engagement",
            rows=data.engagement_events,
            definition=definitions["engagement_events"],
            section_id="analytics-engagement",
            empty_message="No engagement events in this window.",
        )}
        {_render_event_table(
            title="Authoritative conversions",
            rows=data.server_conversion_events,
            definition=definitions["server_conversion_events"],
            section_id="analytics-server",
            empty_message="No server conversion events in this window.",
        )}
      </div>
      {_render_event_table(
          title="Client UX signals (non-authoritative)",
          rows=data.client_supplementary_events,
          definition=definitions["client_supplementary_events"],
          section_id="analytics-client",
          empty_message="No supplementary client events in this window.",
      )}
      <section class="dashboard-panel" aria-labelledby="analytics-rates-title">
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
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-event-attr-title">
        <h2 class="admin-section-title" id="analytics-event-attr-title">Event attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["event_attribution"])}</p>
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
            <tbody>{_render_event_attribution_rows(data.event_attribution)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-lead-attr-title">
        <h2 class="admin-section-title" id="analytics-lead-attr-title">Lead &amp; payment attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["lead_attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Medium</th>
                <th scope="col">Campaign</th>
                <th scope="col">Leads</th>
                <th scope="col">Paid</th>
              </tr>
            </thead>
            <tbody>{_render_lead_attribution_rows(data.lead_attribution)}</tbody>
          </table>
        </div>
      </section>
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-case-studies-title">
          <h2 class="admin-section-title" id="analytics-case-studies-title">Case study views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["case_study_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table dashboard-count-table">
              <thead><tr><th scope="col">Slug</th><th scope="col">Views</th></tr></thead>
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views in this window.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["article_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table dashboard-count-table">
              <thead><tr><th scope="col">Slug</th><th scope="col">Views</th></tr></thead>
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
