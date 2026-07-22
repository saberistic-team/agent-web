"""HTML for the marketing analytics admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics import (
    AttributionRow,
    ContentEngagementRow,
    ConversionRate,
    EventCount,
    MarketingAnalyticsData,
    VALID_PERIOD_DAYS,
    marketing_analytics_is_empty,
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return _esc(value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC"))


def _format_rate(rate: ConversionRate) -> str:
    if rate.rate_pct is None:
        return "—"
    return f"{rate.rate_pct}%"


def _render_period_selector(period_days: int) -> str:
    options = []
    for days in sorted(VALID_PERIOD_DAYS):
        selected = " selected" if days == period_days else ""
        options.append(
            f'<option value="{days}"{selected}>Last {days} days</option>'
        )
    return f"""<form class="dashboard-period-form" method="get" action="/admin/analytics">
      <label class="dashboard-period-label" for="analytics-period">Period
        <select id="analytics-period" name="period" onchange="this.form.submit()">
          {"".join(options)}
        </select>
      </label>
      <noscript><button class="admin-action admin-action--secondary" type="submit">Apply</button></noscript>
    </form>"""


def _render_event_rows(rows: tuple[EventCount, ...], *, empty_message: str) -> str:
    if not rows or not any(row.count for row in rows):
        return f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.label)}</td>
          <td><span class="analytics-source analytics-source--{row.source}">{_esc(row.source.title())}</span></td>
          <td>{row.count}</td>
        </tr>"""
        for row in rows
        if row.count > 0
    ) or f'<tr><td colspan="3" class="audit-empty">{_esc(empty_message)}</td></tr>'


def _render_conversion_rows(rows: tuple[ConversionRate, ...]) -> str:
    if not rows:
        return '<tr><td colspan="4" class="audit-empty">No conversion data in this period.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(rate.label)}</td>
          <td>{rate.numerator}</td>
          <td>{rate.denominator}</td>
          <td>{_format_rate(rate)}</td>
        </tr>
        <tr class="analytics-rate-def">
          <td colspan="4">
            <span class="dashboard-metric-def">
              {_esc(rate.numerator_definition)} ÷ {_esc(rate.denominator_definition)}
            </span>
          </td>
        </tr>"""
        for rate in rows
    )


def _render_attribution_rows(rows: tuple[AttributionRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="6" class="audit-empty">No attributed traffic in this period.</td></tr>'
    return "".join(
        f"""<tr>
          <td>{_esc(row.utm_source)}</td>
          <td>{_esc(row.utm_medium)}</td>
          <td>{_esc(row.utm_campaign)}</td>
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


def _render_funnel_chart(data: MarketingAnalyticsData) -> str:
    """Simple CSS bar chart for top-of-funnel steps."""
    steps: list[tuple[str, int]] = []
    for row in data.engagement_counts:
        if row.event_name in {
            "Landing Viewed",
            "Brief Viewed",
            "Brief Form Started",
            "Contact Initiated",
        }:
            steps.append((row.label, row.count))
    for row in data.server_conversion_counts:
        steps.append((row.label, row.count))
    if not steps or max(count for _, count in steps) == 0:
        return ""
    peak = max(count for _, count in steps)
    bars = []
    for label, count in steps:
        width = round(100.0 * count / peak) if peak else 0
        bars.append(
            f"""<div class="analytics-funnel-row">
              <span class="analytics-funnel-label">{_esc(label)}</span>
              <div class="analytics-funnel-bar-wrap" aria-hidden="true">
                <div class="analytics-funnel-bar" style="width:{width}%"></div>
              </div>
              <span class="analytics-funnel-count">{count}</span>
            </div>"""
        )
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-funnel-title">
      <h2 class="admin-section-title" id="analytics-funnel-title">Funnel snapshot</h2>
      <p class="dashboard-metric-def">Relative volume by step — browser engagement then server conversions.</p>
      <div class="analytics-funnel-chart">{"".join(bars)}</div>
    </section>"""


def render_marketing_analytics_page(
    *,
    data: MarketingAnalyticsData,
    admin_username: str,
    csrf_token: str = "",
    db_error: bool = False,
    preview_banner: str | None = None,
) -> str:
    generated = _format_timestamp(data.generated_at)
    period_start = _format_timestamp(data.period_start)
    period_end = _format_timestamp(data.period_end)
    definitions = data.metric_definitions
    banner_html = ""
    if preview_banner:
        banner_html = (
            f'<p class="admin-preview-banner" role="status">{_esc(preview_banner)}</p>'
        )

    if db_error:
        body_block = """<p class="brief-error" role="alert">
            Marketing analytics are temporarily unavailable. Try again shortly.
          </p>"""
    elif marketing_analytics_is_empty(data):
        body_block = """<section class="dashboard-empty" aria-labelledby="analytics-empty-title">
          <h2 class="admin-section-title" id="analytics-empty-title">No events yet</h2>
          <p class="admin-lede">
            First-party analytics will populate after site traffic and brief submissions.
            Enable <code>FIRST_PARTY_ANALYTICS_ENABLED</code> in production.
          </p>
        </section>"""
    else:
        body_block = ""

    export_query = f"?period={data.period_days}"
    main = f"""<section class="admin-panel dashboard-root" aria-labelledby="marketing-analytics-title">
      {banner_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="marketing-analytics-title">Funnel &amp; attribution</h1>
      <p class="admin-lede">
        First-party engagement and authoritative conversion metrics for
        <time datetime="{period_start}">{period_start}</time>
        through <time datetime="{period_end}">{period_end}</time>
        ({data.period_days}d, UTC). Generated <time datetime="{generated}">{generated}</time>.
      </p>
      <div class="dashboard-toolbar">
        {_render_period_selector(data.period_days)}
        <a class="dashboard-secondary-link" href="/admin/analytics/export.csv{export_query}">Export CSV</a>
      </div>
      {body_block}
      {_render_funnel_chart(data)}
      <div class="dashboard-grid">
        <section class="dashboard-panel" aria-labelledby="analytics-engagement-title">
          <h2 class="admin-section-title" id="analytics-engagement-title">Browser engagement</h2>
          <p class="dashboard-metric-def">{_esc(definitions["engagement_counts"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Source</th><th>Count</th></tr></thead>
              <tbody>{_render_event_rows(data.engagement_counts, empty_message="No engagement events.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-server-title">
          <h2 class="admin-section-title" id="analytics-server-title">Server conversions</h2>
          <p class="dashboard-metric-def">{_esc(definitions["server_conversions"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Event</th><th>Source</th><th>Count</th></tr></thead>
              <tbody>{_render_event_rows(data.server_conversion_counts, empty_message="No server conversions.")}</tbody>
            </table>
          </div>
        </section>
      </div>
      <section class="dashboard-panel" aria-labelledby="analytics-rates-title">
        <h2 class="admin-section-title" id="analytics-rates-title">Conversion rates</h2>
        <p class="dashboard-metric-def">Rates show numerator ÷ denominator; em dash when denominator is zero.</p>
        <div class="admin-table-wrap">
          <table class="admin-table">
            <thead><tr><th>Step</th><th>Num.</th><th>Denom.</th><th>Rate</th></tr></thead>
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
                <th>Source</th><th>Medium</th><th>Campaign</th>
                <th>Engagement</th><th>Leads</th><th>Payments</th>
              </tr>
            </thead>
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
              <tbody>{_render_content_rows(data.case_study_engagement, empty_message="No case study views.")}</tbody>
            </table>
          </div>
        </section>
        <section class="dashboard-panel" aria-labelledby="analytics-articles-title">
          <h2 class="admin-section-title" id="analytics-articles-title">Insight article views</h2>
          <p class="dashboard-metric-def">{_esc(definitions["content_engagement"])}</p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead><tr><th>Slug</th><th>Views</th></tr></thead>
              <tbody>{_render_content_rows(data.article_engagement, empty_message="No article views.")}</tbody>
            </table>
          </div>
        </section>
      </div>
      <section class="dashboard-panel" aria-labelledby="analytics-abandoned-title">
        <h2 class="admin-section-title" id="analytics-abandoned-title">Abandoned checkouts</h2>
        <p class="dashboard-metric-def">{_esc(definitions["abandoned_checkouts"])}</p>
        <p class="analytics-stat-value">{data.abandoned_checkouts}</p>
      </section>
    </section>"""
    return render_admin_shell(
        title="Analytics",
        main=main,
        active_path="/admin/analytics",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
