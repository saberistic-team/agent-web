"""HTML for the marketing analytics admin dashboard."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.admin_layout import render_admin_shell
from app.marketing_analytics_dashboard import (
    ConversionRate,
    EventCount,
    MarketingAnalyticsDashboardData,
    MAX_RANGE_DAYS,
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


def _render_date_filter(data: MarketingAnalyticsDashboardData) -> str:
    date_from = _esc(data.date_range.date_from_raw or "")
    date_to = _esc(data.date_range.date_to_raw or "")
    export_query = ""
    if data.date_range.date_from_raw:
        export_query += f"date_from={_esc(data.date_range.date_from_raw)}&"
    if data.date_range.date_to_raw:
        export_query += f"date_to={_esc(data.date_range.date_to_raw)}"
    export_href = f"/admin/analytics/export.csv?{export_query.rstrip('&')}"
    return f"""<form class="admin-filter-form analytics-filter-form" method="get" action="/admin/analytics">
        <div class="admin-filter-row">
          <label>
            From
            <input type="date" name="date_from" value="{date_from}" />
          </label>
          <label>
            To
            <input type="date" name="date_to" value="{date_to}" />
          </label>
          <button type="submit" class="admin-filter-submit">Apply</button>
          <a class="dashboard-secondary-link" href="{export_href}">Export CSV</a>
        </div>
        <p class="admin-note">UTC date range, max {MAX_RANGE_DAYS} days. Default is the prior 7 days.</p>
      </form>"""


def _max_count(rows: tuple[EventCount, ...]) -> int:
    return max((row.count for row in rows), default=0) or 1


def _render_event_table(
    *,
    title: str,
    rows: tuple[EventCount, ...],
    definition: str,
    section_id: str,
    source_label: str,
) -> str:
    peak = _max_count(rows)
    body_rows: list[str] = []
    for row in rows:
        width_pct = round(100.0 * row.count / peak, 1) if row.count else 0
        body_rows.append(
            f"""<tr>
              <td>{_esc(row.label)}</td>
              <td>{row.count}</td>
              <td class="analytics-bar-cell">
                <span class="analytics-bar" style="width:{width_pct}%"></span>
              </td>
            </tr>"""
        )
    if not body_rows:
        body_rows.append('<tr><td colspan="3" class="audit-empty">No events in range.</td></tr>')
    return f"""<section class="dashboard-panel" aria-labelledby="{section_id}">
      <h2 class="admin-section-title" id="{section_id}">{_esc(title)}</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <p class="admin-note">Source: {_esc(source_label)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table analytics-event-table">
          <thead><tr><th scope="col">Event</th><th scope="col">Count</th><th scope="col">Share</th></tr></thead>
          <tbody>{"".join(body_rows)}</tbody>
        </table>
      </div>
    </section>"""


def _render_conversion_table(
    rates: tuple[ConversionRate, ...],
    definition: str,
) -> str:
    rows: list[str] = []
    for rate in rates:
        rows.append(
            f"""<tr>
              <td>{_esc(rate.label)}</td>
              <td>{rate.numerator} <span class="admin-note">({_esc(rate.numerator_label)})</span></td>
              <td>{rate.denominator} <span class="admin-note">({_esc(rate.denominator_label)})</span></td>
              <td>{_format_rate(rate)}</td>
            </tr>
            <tr class="analytics-def-row">
              <td colspan="4" class="admin-note">
                {_esc(rate.numerator_definition)} ÷ {_esc(rate.denominator_definition)}
              </td>
            </tr>"""
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="audit-empty">No conversion data in range.</td></tr>')
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-conversion-title">
      <h2 class="admin-section-title" id="analytics-conversion-title">Conversion rates</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th scope="col">Rate</th>
              <th scope="col">Numerator</th>
              <th scope="col">Denominator</th>
              <th scope="col">Result</th>
            </tr>
          </thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
      </div>
    </section>"""


def _render_attribution_table(rows: tuple[Any, ...], definition: str) -> str:
    if rows:
        body = "".join(
            f"""<tr>
              <td>{_esc(row.utm_source)}</td>
              <td>{_esc(row.utm_medium)}</td>
              <td>{_esc(row.utm_campaign)}</td>
              <td>{row.landing_views}</td>
              <td>{row.leads}</td>
              <td>{row.payments}</td>
            </tr>"""
            for row in rows
        )
    else:
        body = '<tr><td colspan="6" class="audit-empty">No attributed traffic in range.</td></tr>'
    return f"""<section class="dashboard-panel" aria-labelledby="analytics-attribution-title">
      <h2 class="admin-section-title" id="analytics-attribution-title">Attribution</h2>
      <p class="dashboard-metric-def">{_esc(definition)}</p>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Medium</th>
              <th scope="col">Campaign</th>
              <th scope="col">Landings</th>
              <th scope="col">Leads</th>
              <th scope="col">Payments</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>"""


def _render_content_table(
    *,
    title: str,
    rows: tuple[Any, ...],
    section_id: str,
    empty_message: str,
) -> str:
    if rows:
        body = "".join(
            f"<tr><td>{_esc(row.slug)}</td><td>{row.views}</td></tr>" for row in rows
        )
    else:
        body = f'<tr><td colspan="2" class="audit-empty">{_esc(empty_message)}</td></tr>'
    return f"""<section class="dashboard-panel" aria-labelledby="{section_id}">
      <h2 class="admin-section-title" id="{section_id}">{_esc(title)}</h2>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th scope="col">Slug</th><th scope="col">Views</th></tr></thead>
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

    range_label = _esc(
        f"{data.date_range.date_from_raw or '—'} to {data.date_range.date_to_raw or '—'} (UTC)"
    )

    main = f"""<section class="admin-panel dashboard-root analytics-root" aria-labelledby="analytics-title">
      {banner_html}
      {error_html}
      <p class="admin-eyebrow">Marketing</p>
      <h1 class="admin-title" id="analytics-title">Analytics</h1>
      <p class="admin-lede">
        First-party funnel, attribution, and content engagement for
        <strong>{range_label}</strong>.
        Generated <time datetime="{generated}">{generated}</time>.
      </p>
      {_render_date_filter(data)}
      <div class="dashboard-grid analytics-grid">
        {_render_event_table(
            title="Browser engagement",
            rows=data.engagement_counts,
            definition=definitions["engagement"],
            section_id="analytics-engagement-title",
            source_label="Client page and nav events (non-authoritative)",
        )}
        {_render_event_table(
            title="Server conversions",
            rows=data.server_counts,
            definition=definitions["server"],
            section_id="analytics-server-title",
            source_label="Authoritative CRM and Stripe-backed events",
        )}
      </div>
      {_render_event_table(
          title="Supplementary client signals",
          rows=data.supplementary_counts,
          definition="Client-only UX signals that mirror funnel steps but are not used as conversion truth.",
          section_id="analytics-supplementary-title",
          source_label="Browser (Checkout Cancelled, Brief Success Viewed)",
      )}
      {_render_conversion_table(data.conversion_rates, definitions["conversion"])}
      {_render_attribution_table(data.attribution_rows, definitions["attribution"])}
      <div class="dashboard-grid analytics-grid">
        {_render_content_table(
            title="Top insights",
            rows=data.article_engagement,
            section_id="analytics-articles-title",
            empty_message="No insight views in range.",
        )}
        {_render_content_table(
            title="Top case studies",
            rows=data.case_study_engagement,
            section_id="analytics-case-studies-title",
            empty_message="No case study views in range.",
        )}
      </div>
      <p class="dashboard-metric-def">{_esc(definitions["content"])}</p>
    </section>"""

    return render_admin_shell(
        title="Analytics",
        main=main,
        active_path="/admin/analytics",
        admin_username=admin_username,
        csrf_token=csrf_token,
    )
