"""Marketing analytics dashboard — explicit metric definitions and load helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

import psycopg

from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_CASE_STUDY_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_CONTACT_INITIATED,
    EVENT_INSIGHT_VIEWED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    EVENT_SERVICES_VIEWED,
)
from app.repositories.protocols import AnalyticsDashboardRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 90
DEFAULT_ATTRIBUTION_LIMIT = 20
DEFAULT_CONTENT_LIMIT = 15

METRIC_EVENT_WINDOW = (
    f"Distinct analytics_events rows with occurred_at in [{DASHBOARD_TIMEZONE}] "
    "inclusive start, exclusive end of the selected date range. "
    "Duplicates are rejected at ingest via idempotency_key; bot traffic is blocked at ingest."
)
METRIC_ENGAGEMENT_EVENTS = (
    "Browser-reported page and nav engagement from POST /api/events. "
    "Not authoritative for revenue or lead counts."
)
METRIC_SERVER_CONVERSIONS = (
    "Server-emitted events after CRM persistence or Stripe webhook confirmation. "
    "Authoritative for lead, checkout, and payment counts."
)
METRIC_CONVERSION_RATE = (
    "Percentage = 100 × numerator ÷ denominator, rounded to one decimal. "
    "Shows em dash when denominator is zero."
)
METRIC_ATTRIBUTION = (
    "Aggregated counts grouped by allowlisted utm_source, utm_medium, and utm_campaign "
    "from the attribution JSONB column. No individual session or visitor identifiers."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Aggregated view counts by server-known slug from analytics_events properties. "
    "No per-visitor browsing history."
)

ENGAGEMENT_EVENT_LABELS: dict[str, str] = {
    EVENT_LANDING_VIEWED: "Landing viewed",
    EVENT_SERVICES_VIEWED: "Services viewed",
    EVENT_CASE_STUDIES_VIEWED: "Case studies index viewed",
    EVENT_CASE_STUDY_VIEWED: "Case study viewed",
    EVENT_INSIGHTS_VIEWED: "Insights index viewed",
    EVENT_INSIGHT_VIEWED: "Insight viewed",
    EVENT_BRIEF_VIEWED: "Brief viewed",
    EVENT_BRIEF_FORM_STARTED: "Brief form started",
    EVENT_CONTACT_INITIATED: "Contact initiated",
}

SERVER_EVENT_LABELS: dict[str, str] = {
    EVENT_LEAD_PERSISTED: "Lead persisted",
    EVENT_CHECKOUT_OPENED: "Checkout opened",
    EVENT_PAYMENT_COMPLETED: "Payment completed",
}

ENGAGEMENT_EVENT_ORDER = tuple(ENGAGEMENT_EVENT_LABELS)
SERVER_EVENT_ORDER = tuple(SERVER_EVENT_LABELS)

EventSource = Literal["browser", "server"]


@dataclass(frozen=True)
class AnalyticsDateRange:
    start: datetime
    end: datetime
    from_date: date
    to_date: date
    from_raw: str
    to_raw: str


@dataclass(frozen=True)
class EventCount:
    event_name: str
    label: str
    count: int
    source: EventSource


@dataclass(frozen=True)
class ConversionRate:
    key: str
    label: str
    numerator: int
    denominator: int
    numerator_label: str
    denominator_label: str
    numerator_source: EventSource
    denominator_source: EventSource
    rate_pct: float | None


@dataclass(frozen=True)
class AttributionRow:
    source: str
    medium: str
    campaign: str
    total_events: int
    leads: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    content_type: Literal["case_study", "article"]
    views: int


@dataclass(frozen=True)
class AnalyticsDashboardData:
    engagement_counts: tuple[EventCount, ...]
    server_counts: tuple[EventCount, ...]
    conversion_rates: tuple[ConversionRate, ...]
    attribution: tuple[AttributionRow, ...]
    case_studies: tuple[ContentEngagementRow, ...]
    articles: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    date_range: AnalyticsDateRange
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "event_window": METRIC_EVENT_WINDOW,
            "engagement_events": METRIC_ENGAGEMENT_EVENTS,
            "server_conversions": METRIC_SERVER_CONVERSIONS,
            "conversion_rate": METRIC_CONVERSION_RATE,
            "attribution": METRIC_ATTRIBUTION,
            "content_engagement": METRIC_CONTENT_ENGAGEMENT,
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


def parse_date_range(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse and bound an inclusive UTC date range for dashboard queries."""
    reference = now or datetime.now(timezone.utc)
    today = reference.date()
    default_from = today - timedelta(days=DEFAULT_RANGE_DAYS - 1)

    parsed_from = _parse_date_param(date_from) or default_from
    parsed_to = _parse_date_param(date_to) or today
    if parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from

    span_days = (parsed_to - parsed_from).days + 1
    if span_days > MAX_RANGE_DAYS:
        parsed_from = parsed_to - timedelta(days=MAX_RANGE_DAYS - 1)

    start = datetime.combine(parsed_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(parsed_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return AnalyticsDateRange(
        start=start,
        end=end,
        from_date=parsed_from,
        to_date=parsed_to,
        from_raw=parsed_from.isoformat(),
        to_raw=parsed_to.isoformat(),
    )


def compute_rate_pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _event_count(
    counts: dict[str, int],
    event_name: str,
    *,
    label: str,
    source: EventSource,
) -> EventCount:
    return EventCount(
        event_name=event_name,
        label=label,
        count=int(counts.get(event_name, 0)),
        source=source,
    )


def build_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRate, ...]:
    """Build funnel conversion rates with explicit numerator/denominator labels."""

    def _rate(
        *,
        key: str,
        label: str,
        numerator_event: str,
        denominator_event: str,
        numerator_label: str,
        denominator_label: str,
        numerator_source: EventSource,
        denominator_source: EventSource,
    ) -> ConversionRate:
        numerator = int(counts.get(numerator_event, 0))
        denominator = int(counts.get(denominator_event, 0))
        return ConversionRate(
            key=key,
            label=label,
            numerator=numerator,
            denominator=denominator,
            numerator_label=numerator_label,
            denominator_label=denominator_label,
            numerator_source=numerator_source,
            denominator_source=denominator_source,
            rate_pct=compute_rate_pct(numerator, denominator),
        )

    return (
        _rate(
            key="landing_to_brief",
            label="Landing → brief view",
            numerator_event=EVENT_BRIEF_VIEWED,
            denominator_event=EVENT_LANDING_VIEWED,
            numerator_label=ENGAGEMENT_EVENT_LABELS[EVENT_BRIEF_VIEWED],
            denominator_label=ENGAGEMENT_EVENT_LABELS[EVENT_LANDING_VIEWED],
            numerator_source="browser",
            denominator_source="browser",
        ),
        _rate(
            key="brief_to_form",
            label="Brief view → form start",
            numerator_event=EVENT_BRIEF_FORM_STARTED,
            denominator_event=EVENT_BRIEF_VIEWED,
            numerator_label=ENGAGEMENT_EVENT_LABELS[EVENT_BRIEF_FORM_STARTED],
            denominator_label=ENGAGEMENT_EVENT_LABELS[EVENT_BRIEF_VIEWED],
            numerator_source="browser",
            denominator_source="browser",
        ),
        _rate(
            key="form_to_lead",
            label="Form start → lead",
            numerator_event=EVENT_LEAD_PERSISTED,
            denominator_event=EVENT_BRIEF_FORM_STARTED,
            numerator_label=SERVER_EVENT_LABELS[EVENT_LEAD_PERSISTED],
            denominator_label=ENGAGEMENT_EVENT_LABELS[EVENT_BRIEF_FORM_STARTED],
            numerator_source="server",
            denominator_source="browser",
        ),
        _rate(
            key="lead_to_checkout",
            label="Lead → checkout",
            numerator_event=EVENT_CHECKOUT_OPENED,
            denominator_event=EVENT_LEAD_PERSISTED,
            numerator_label=SERVER_EVENT_LABELS[EVENT_CHECKOUT_OPENED],
            denominator_label=SERVER_EVENT_LABELS[EVENT_LEAD_PERSISTED],
            numerator_source="server",
            denominator_source="server",
        ),
        _rate(
            key="checkout_to_payment",
            label="Checkout → payment",
            numerator_event=EVENT_PAYMENT_COMPLETED,
            denominator_event=EVENT_CHECKOUT_OPENED,
            numerator_label=SERVER_EVENT_LABELS[EVENT_PAYMENT_COMPLETED],
            denominator_label=SERVER_EVENT_LABELS[EVENT_CHECKOUT_OPENED],
            numerator_source="server",
            denominator_source="server",
        ),
        _rate(
            key="landing_to_payment",
            label="Landing → payment",
            numerator_event=EVENT_PAYMENT_COMPLETED,
            denominator_event=EVENT_LANDING_VIEWED,
            numerator_label=SERVER_EVENT_LABELS[EVENT_PAYMENT_COMPLETED],
            denominator_label=ENGAGEMENT_EVENT_LABELS[EVENT_LANDING_VIEWED],
            numerator_source="server",
            denominator_source="browser",
        ),
    )


def load_analytics_dashboard(
    conn: psycopg.Connection,
    repo: AnalyticsDashboardRepository,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
    attribution_limit: int = DEFAULT_ATTRIBUTION_LIMIT,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
) -> AnalyticsDashboardData:
    """Load marketing analytics for the bounded date range."""
    reference = now or datetime.now(timezone.utc)
    date_range = parse_date_range(date_from=date_from, date_to=date_to, now=reference)

    tracked_events = ENGAGEMENT_EVENT_ORDER + SERVER_EVENT_ORDER
    counts = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=tracked_events,
    )

    engagement_counts = tuple(
        _event_count(counts, name, label=ENGAGEMENT_EVENT_LABELS[name], source="browser")
        for name in ENGAGEMENT_EVENT_ORDER
    )
    server_counts = tuple(
        _event_count(counts, name, label=SERVER_EVENT_LABELS[name], source="server")
        for name in SERVER_EVENT_ORDER
    )

    attribution_rows = repo.list_attribution_breakdown(
        conn,
        start=date_range.start,
        end=date_range.end,
        limit=attribution_limit,
    )
    case_study_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=content_limit,
    )
    article_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_INSIGHT_VIEWED,
        slug_property="article_slug",
        limit=content_limit,
    )

    return AnalyticsDashboardData(
        engagement_counts=engagement_counts,
        server_counts=server_counts,
        conversion_rates=build_conversion_rates(counts),
        attribution=tuple(
            AttributionRow(
                source=str(row["source"]),
                medium=str(row["medium"]),
                campaign=str(row["campaign"]),
                total_events=int(row["total_events"]),
                leads=int(row["leads"]),
            )
            for row in attribution_rows
        ),
        case_studies=tuple(
            ContentEngagementRow(slug=str(row["slug"]), content_type="case_study", views=int(row["views"]))
            for row in case_study_rows
        ),
        articles=tuple(
            ContentEngagementRow(slug=str(row["slug"]), content_type="article", views=int(row["views"]))
            for row in article_rows
        ),
        generated_at=reference,
        date_range=date_range,
    )


def dashboard_has_data(data: AnalyticsDashboardData) -> bool:
    """True when any event, attribution, or content row has non-zero activity."""
    if any(row.count for row in data.engagement_counts):
        return True
    if any(row.count for row in data.server_counts):
        return True
    if data.attribution:
        return True
    if data.case_studies or data.articles:
        return True
    return False
