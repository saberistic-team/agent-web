"""Admin HTML for the first-party marketing analytics dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics_dashboard import (
    AnalyticsDashboardData,
    AttributionRow,
    ContentEngagementRow,
    ConversionRateRow,
    EventVolumeRow,
    dashboard_has_activity,
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
    return f"{row.rate_pct:.1f}%"


def _source_badge(source: str) -> str:
    label = "Server" if source == "server" else "Browser"
    modifier = "analytics-badge--server" if source == "server" else "analytics-badge--browser"
    return f'<span class="admin-badge analytics-badge {modifier}">{label}</span>'


def _render_event_rows(rows: tuple[EventVolumeRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="3" class="audit-empty">No events in range.</td></tr>'
    max_count = max((row.count for row in rows), default=1) or 1
    body: list[str] = []
    for row in rows:
        width = 0 if row.count == 0 else max(4, round(100 * row.count / max_count))
        body.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{row.count}</td>
              <td>{_source_badge(row.source)}</td>
            </tr>
            <tr class="analytics-funnel-bar-row">
              <td colspan="3">
                <div class="analytics-funnel-bar" style="width:{width}%;" aria-hidden="true"></div>
              </td>
            </tr>"""
        )
    return "".join(body)


def _render_conversion_rows(rows: tuple[ConversionRateRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No conversion data.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.label)}</td>
          <td>{row.numerator}</td>
          <td>{row.denominator}</td>
          <td>{_format_rate(row)}</td>
        </tr>
        <tr class="analytics-rate-def">
          <td colspan="4"><p class="dashboard-metric-def">{_esc(row.definition)}</p></td>
        </tr>"""
        for row in rows
    )


def _render_attribution_rows(rows: tuple[AttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="8" class="audit-empty">No attributed events in range.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.utm_source)}</td>
          <td>{_esc(row.utm_medium)}</td>
          <td>{_esc(row.utm_campaign)}</td>
          <td>{row.landing_views}</td>
          <td>{row.brief_starts}</td>
          <td>{row.leads}</td>
          <td>{row.checkouts}</td>
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


def render_analytics_dashboard_page(
    *,
    data: AnalyticsDashboardData,
    admin_username: str,
    csrf_token: str = "",
    db_error: bool = False,
    preview_banner: str | None = None,
) -> str:
    definitions = data.metric_definitions
    from_value = data.date_from.isoformat()
    to_value = data.date_to.isoformat()
    export_href = f"/admin/analytics/export.csv?from={_esc(from_value)}&amp;to={_esc(to_value)}"
    generated = _format_timestamp(data.generated_at)

    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if db_error:
        body_block = """<p class="brief-error" role="alert">
            Analytics metrics are temporarily unavailable. Try again shortly.
          </p>"""
    elif not dashboard_has_activity(data):
        body_block = """<section class="dashboard-empty" aria-labelledby="analytics-empty-title">
          <h2 class="admin-section-title" id="analytics-empty-title">No events yet</h2>
          <p class="admin-lede">
            First-party analytics events will appear here once traffic is recorded for the selected range.
          </p>
        </section>"""
    else:
        body_block = ""

    main = f"""<section class="admin-panel dashboard-root analytics-root" aria-labelledby="analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Analytics</h1>
      <p class="admin-lede">
        Funnel, attribution, and content engagement for saberistic.com.
        All times {_esc("UTC")}. Generated <time datetime="{generated}">{generated}</time>.
      </p>
      <form class="analytics-range-form" method="get" action="/admin/analytics">
        <label class="analytics-range-label">From
          <input type="date" name="from" value="{_esc(from_value)}" required />
        </label>
        <label class="analytics-range-label">To
          <input type="date" name="to" value="{_esc(to_value)}" required />
        </label>
        <button class="admin-action" type="submit">Apply range</button>
        <a class="dashboard-secondary-link" href="{export_href}">Export CSV</a>
      </form>
      {body_block}
      <section class="dashboard-panel" aria-labelledby="analytics-events-title">
        <h2 class="admin-section-title" id="analytics-events-title">Event volume</h2>
        <p class="dashboard-metric-def">{_esc(definitions["event_volume"])}</p>
        <p class="dashboard-metric-def">{_esc(definitions["server_vs_browser"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Event</th><th>Count</th><th>Source</th></tr></thead>
            <tbody>{_render_event_rows(data.event_volumes)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-conversion-title">
        <h2 class="admin-section-title" id="analytics-conversion-title">Conversion rates</h2>
        <p class="dashboard-metric-def">{_esc(definitions["conversion_rate"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Step</th><th>Numerator</th><th>Denominator</th><th>Rate</th></tr></thead>
            <tbody>{_render_conversion_rows(data.conversion_rates)}</tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
        <h2 class="admin-section-title" id="analytics-attribution-title">UTM attribution</h2>
        <p class="dashboard-metric-def">{_esc(definitions["attribution"])}</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr>
              <th>Source</th><th>Medium</th><th>Campaign</th>
              <th>Landing</th><th>Brief start</th><th>Lead</th><th>Checkout</th><th>Paid</th>
            </tr></thead>
            <tbody>{_render_attribution_rows(data.attribution_rows)}</tbody>
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
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views in range.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight article views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
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
