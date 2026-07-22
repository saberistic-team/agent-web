"""Marketing analytics dashboard — funnel, attribution, and content engagement."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

import psycopg

from app.analytics_event_schema import (
    EVENT_ABOUT_VIEWED,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_SUCCESS_VIEWED,
    EVENT_BRIEF_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_CASE_STUDY_VIEWED,
    EVENT_CHECKOUT_CANCELLED,
    EVENT_CHECKOUT_OPENED,
    EVENT_CONTACT_INITIATED,
    EVENT_INSIGHT_VIEWED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    EVENT_SERVICES_VIEWED,
)
from app.repositories.protocols import MarketingAnalyticsRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_DATE_RANGE_DAYS = 7
MAX_DATE_RANGE_DAYS = 90
DEFAULT_LIST_LIMIT = 20

METRIC_ENGAGEMENT_EVENTS = (
    "Distinct analytics_events rows in the selected UTC date window filtered by occurred_at "
    "(not received_at). Late-arriving events count in the week they occurred. Rows are "
    "deduplicated at ingest via idempotency_key; bot/noise traffic is rejected before insert."
)
METRIC_SERVER_CONVERSION_EVENTS = (
    "Authoritative server-written conversion events in analytics_events for the UTC window. "
    "Use these counts for funnel truth — not client-only Brief Success Viewed or "
    "Checkout Cancelled events."
)
METRIC_CLIENT_SUPPLEMENTARY_EVENTS = (
    "Non-authoritative browser events that mirror checkout/payment UX. Shown for context only; "
    "conversion rates and revenue truth use server events and project_briefs."
)
METRIC_CONVERSION_RATES = (
    "Rates use explicit numerators and denominators from the tables above. Zero denominators "
    "render as em dash (—). All windows use UTC midnight boundaries."
)
METRIC_EVENT_ATTRIBUTION = (
    "Allowlisted utm_source, utm_medium, and utm_campaign from analytics_events.attribution "
    "JSONB only. NULL/blank values bucket as (direct) or (none)."
)
METRIC_LEAD_ATTRIBUTION = (
    "Leads and paid diagnostics from project_briefs.created_at / paid_at in the UTC window, "
    "grouped by persisted utm_* columns (same allowlist as browser capture)."
)
METRIC_CASE_STUDY_ENGAGEMENT = (
    "Aggregated Case Study Viewed counts by server-known case_study_slug — no per-visitor "
    "session history."
)
METRIC_ARTICLE_ENGAGEMENT = (
    "Aggregated Insight Viewed counts by server-known article_slug — no per-visitor session "
    "history."
)

EventSource = Literal["browser", "server", "client_supplementary"]

ENGAGEMENT_EVENTS: tuple[str, ...] = (
    EVENT_LANDING_VIEWED,
    EVENT_ABOUT_VIEWED,
    EVENT_SERVICES_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_CASE_STUDY_VIEWED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_INSIGHT_VIEWED,
    EVENT_BRIEF_VIEWED,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CONTACT_INITIATED,
)

SERVER_CONVERSION_EVENTS: tuple[str, ...] = (
    EVENT_LEAD_PERSISTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_PAYMENT_COMPLETED,
)

CLIENT_SUPPLEMENTARY_EVENTS: tuple[str, ...] = (
    EVENT_BRIEF_SUCCESS_VIEWED,
    EVENT_CHECKOUT_CANCELLED,
)

_CONVERSION_RATE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "landing_to_brief_form",
        "Landing → brief form start",
        EVENT_BRIEF_FORM_STARTED,
        EVENT_LANDING_VIEWED,
    ),
    (
        "brief_view_to_form",
        "Brief view → form start",
        EVENT_BRIEF_FORM_STARTED,
        EVENT_BRIEF_VIEWED,
    ),
    (
        "form_to_lead",
        "Brief form → lead persisted",
        EVENT_LEAD_PERSISTED,
        EVENT_BRIEF_FORM_STARTED,
    ),
    (
        "lead_to_checkout",
        "Lead → checkout opened",
        EVENT_CHECKOUT_OPENED,
        EVENT_LEAD_PERSISTED,
    ),
    (
        "checkout_to_payment",
        "Checkout → payment completed",
        EVENT_PAYMENT_COMPLETED,
        EVENT_CHECKOUT_OPENED,
    ),
)


@dataclass(frozen=True)
class AnalyticsDateRange:
    date_from: date
    date_to: date
    window_start: datetime
    window_end: datetime
    date_from_raw: str
    date_to_raw: str


@dataclass(frozen=True)
class EventCountRow:
    event_name: str
    count: int
    source: EventSource


@dataclass(frozen=True)
class ConversionRateRow:
    key: str
    label: str
    numerator_event: str
    denominator_event: str
    numerator: int
    denominator: int
    rate_pct: float | None
    definition: str


@dataclass(frozen=True)
class EventAttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    event_count: int


@dataclass(frozen=True)
class LeadAttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    leads: int
    payments: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    views: int


@dataclass(frozen=True)
class MarketingAnalyticsDashboardData:
    date_range: AnalyticsDateRange
    engagement_events: tuple[EventCountRow, ...]
    server_conversion_events: tuple[EventCountRow, ...]
    client_supplementary_events: tuple[EventCountRow, ...]
    conversion_rates: tuple[ConversionRateRow, ...]
    event_attribution: tuple[EventAttributionRow, ...]
    lead_attribution: tuple[LeadAttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "engagement_events": METRIC_ENGAGEMENT_EVENTS,
            "server_conversion_events": METRIC_SERVER_CONVERSION_EVENTS,
            "client_supplementary_events": METRIC_CLIENT_SUPPLEMENTARY_EVENTS,
            "conversion_rates": METRIC_CONVERSION_RATES,
            "event_attribution": METRIC_EVENT_ATTRIBUTION,
            "lead_attribution": METRIC_LEAD_ATTRIBUTION,
            "case_study_engagement": METRIC_CASE_STUDY_ENGAGEMENT,
            "article_engagement": METRIC_ARTICLE_ENGAGEMENT,
        }
    )


def _parse_date_param(value: str | None) -> date | None:
    if value is None:
        return None
    trimmed = value.strip()[:10]
    if not trimmed:
        return None
    try:
        return date.fromisoformat(trimmed)
    except ValueError:
        return None


def _utc_window_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    window_start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    window_end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return window_start, window_end


def parse_analytics_date_range(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    reference: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse bounded UTC date range for marketing analytics queries."""
    now = reference or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    parsed_to = _parse_date_param(date_to) or today
    default_from = parsed_to - timedelta(days=DEFAULT_DATE_RANGE_DAYS - 1)
    parsed_from = _parse_date_param(date_from) or default_from
    if parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from
    span_days = (parsed_to - parsed_from).days + 1
    if span_days > MAX_DATE_RANGE_DAYS:
        parsed_from = parsed_to - timedelta(days=MAX_DATE_RANGE_DAYS - 1)
    window_start, window_end = _utc_window_bounds(parsed_from, parsed_to)
    return AnalyticsDateRange(
        date_from=parsed_from,
        date_to=parsed_to,
        window_start=window_start,
        window_end=window_end,
        date_from_raw=parsed_from.isoformat(),
        date_to_raw=parsed_to.isoformat(),
    )


def compute_conversion_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _conversion_rate_definition(numerator_event: str, denominator_event: str) -> str:
    return (
        f"Numerator: count of `{numerator_event}` in window. "
        f"Denominator: count of `{denominator_event}` in window."
    )


def _event_counts_for_names(
    counts: dict[str, int],
    event_names: tuple[str, ...],
    *,
    source: EventSource,
) -> tuple[EventCountRow, ...]:
    return tuple(
        EventCountRow(event_name=name, count=int(counts.get(name, 0)), source=source)
        for name in event_names
    )


def _build_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRateRow, ...]:
    rows: list[ConversionRateRow] = []
    for key, label, numerator_event, denominator_event in _CONVERSION_RATE_SPECS:
        numerator = int(counts.get(numerator_event, 0))
        denominator = int(counts.get(denominator_event, 0))
        rows.append(
            ConversionRateRow(
                key=key,
                label=label,
                numerator_event=numerator_event,
                denominator_event=denominator_event,
                numerator=numerator,
                denominator=denominator,
                rate_pct=compute_conversion_rate(numerator, denominator),
                definition=_conversion_rate_definition(numerator_event, denominator_event),
            )
        )
    return tuple(rows)


def _parse_event_attribution(rows: list[dict[str, Any]]) -> tuple[EventAttributionRow, ...]:
    return tuple(
        EventAttributionRow(
            utm_source=str(row["utm_source"]),
            utm_medium=str(row["utm_medium"]),
            utm_campaign=str(row["utm_campaign"]),
            event_count=int(row["event_count"]),
        )
        for row in rows
    )


def _parse_lead_attribution(rows: list[dict[str, Any]]) -> tuple[LeadAttributionRow, ...]:
    return tuple(
        LeadAttributionRow(
            utm_source=str(row["utm_source"]),
            utm_medium=str(row["utm_medium"]),
            utm_campaign=str(row["utm_campaign"]),
            leads=int(row["leads"]),
            payments=int(row["payments"]),
        )
        for row in rows
    )


def _parse_content_engagement(rows: list[dict[str, Any]]) -> tuple[ContentEngagementRow, ...]:
    return tuple(
        ContentEngagementRow(slug=str(row["slug"]), views=int(row["views"]))
        for row in rows
        if row.get("slug")
    )


def load_marketing_analytics_dashboard(
    conn: psycopg.Connection,
    repo: MarketingAnalyticsRepository,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
    list_limit: int = DEFAULT_LIST_LIMIT,
) -> MarketingAnalyticsDashboardData:
    """Load marketing analytics sections for the selected UTC date window."""
    reference = now or datetime.now(timezone.utc)
    date_range = parse_analytics_date_range(
        date_from=date_from,
        date_to=date_to,
        reference=reference,
    )
    all_event_names = (
        *ENGAGEMENT_EVENTS,
        *SERVER_CONVERSION_EVENTS,
        *CLIENT_SUPPLEMENTARY_EVENTS,
    )
    raw_counts = repo.count_events_by_name(
        conn,
        window_start=date_range.window_start,
        window_end=date_range.window_end,
        event_names=all_event_names,
    )
    counts = {name: total for name, total in raw_counts}
    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=_event_counts_for_names(
            counts, ENGAGEMENT_EVENTS, source="browser"
        ),
        server_conversion_events=_event_counts_for_names(
            counts, SERVER_CONVERSION_EVENTS, source="server"
        ),
        client_supplementary_events=_event_counts_for_names(
            counts, CLIENT_SUPPLEMENTARY_EVENTS, source="client_supplementary"
        ),
        conversion_rates=_build_conversion_rates(counts),
        event_attribution=_parse_event_attribution(
            repo.list_event_attribution(
                conn,
                window_start=date_range.window_start,
                window_end=date_range.window_end,
                limit=list_limit,
            )
        ),
        lead_attribution=_parse_lead_attribution(
            repo.list_lead_attribution(
                conn,
                window_start=date_range.window_start,
                window_end=date_range.window_end,
                limit=list_limit,
            )
        ),
        case_study_engagement=_parse_content_engagement(
            repo.list_case_study_engagement(
                conn,
                window_start=date_range.window_start,
                window_end=date_range.window_end,
                limit=list_limit,
            )
        ),
        article_engagement=_parse_content_engagement(
            repo.list_article_engagement(
                conn,
                window_start=date_range.window_start,
                window_end=date_range.window_end,
                limit=list_limit,
            )
        ),
        generated_at=reference,
    )


def dashboard_is_empty(data: MarketingAnalyticsDashboardData) -> bool:
    """True when every section has zero rows or counts."""
    event_sections = (
        data.engagement_events,
        data.server_conversion_events,
        data.client_supplementary_events,
    )
    if any(row.count > 0 for section in event_sections for row in section):
        return False
    if data.event_attribution or data.lead_attribution:
        return False
    if data.case_study_engagement or data.article_engagement:
        return False
    return True


def render_analytics_csv(data: MarketingAnalyticsDashboardData) -> str:
    """Render aggregated dashboard metrics as CSV (no raw events or session IDs)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "metric", "value", "numerator", "denominator", "notes"])
    date_label = f"{data.date_range.date_from_raw}..{data.date_range.date_to_raw} UTC"
    writer.writerow(["meta", "date_range", date_label, "", "", DASHBOARD_TIMEZONE])

    for row in data.engagement_events:
        writer.writerow(["engagement", row.event_name, row.count, "", "", "browser"])
    for row in data.server_conversion_events:
        writer.writerow(["server_conversion", row.event_name, row.count, "", "", "authoritative"])
    for row in data.client_supplementary_events:
        writer.writerow(
            ["client_supplementary", row.event_name, row.count, "", "", "non-authoritative"]
        )
    for row in data.conversion_rates:
        rate = "" if row.rate_pct is None else f"{row.rate_pct}%"
        writer.writerow(
            [
                "conversion_rate",
                row.label,
                rate,
                row.numerator,
                row.denominator,
                row.definition,
            ]
        )
    for row in data.event_attribution:
        writer.writerow(
            [
                "event_attribution",
                row.utm_source,
                row.event_count,
                row.utm_medium,
                row.utm_campaign,
                "events",
            ]
        )
    for row in data.lead_attribution:
        writer.writerow(
            [
                "lead_attribution",
                row.utm_source,
                row.leads,
                row.utm_medium,
                row.utm_campaign,
                f"payments={row.payments}",
            ]
        )
    for row in data.case_study_engagement:
        writer.writerow(["case_study", row.slug, row.views, "", "", "aggregated"])
    for row in data.article_engagement:
        writer.writerow(["article", row.slug, row.views, "", "", "aggregated"])
    return buffer.getvalue()
