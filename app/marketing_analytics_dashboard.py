"""Marketing analytics dashboard — funnel, attribution, and content engagement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import psycopg

from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_CASE_STUDY_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_CONTACT_INITIATED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_INSIGHT_VIEWED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    EVENT_SERVICES_VIEWED,
    UTM_ATTRIBUTION_KEYS,
)
from app.repositories.protocols import MarketingAnalyticsRepository

DEFAULT_DATE_RANGE_DAYS = 7
MAX_DATE_RANGE_DAYS = 90
DEFAULT_LIST_LIMIT = 20
DASHBOARD_TIMEZONE = "UTC"

# Dashboard event rows in display order (label, event_name, authoritative).
DASHBOARD_EVENT_ROWS: tuple[tuple[str, str, bool], ...] = (
    ("Landing views", EVENT_LANDING_VIEWED, False),
    ("Services views", EVENT_SERVICES_VIEWED, False),
    ("Case studies index", EVENT_CASE_STUDIES_VIEWED, False),
    ("Case study views", EVENT_CASE_STUDY_VIEWED, False),
    ("Insights index", EVENT_INSIGHTS_VIEWED, False),
    ("Insight views", EVENT_INSIGHT_VIEWED, False),
    ("Brief views", EVENT_BRIEF_VIEWED, False),
    ("Brief form starts", EVENT_BRIEF_FORM_STARTED, False),
    ("Leads persisted", EVENT_LEAD_PERSISTED, True),
    ("Checkouts opened", EVENT_CHECKOUT_OPENED, True),
    ("Payments completed", EVENT_PAYMENT_COMPLETED, True),
    ("Contact initiated", EVENT_CONTACT_INITIATED, False),
)

DASHBOARD_EVENT_NAMES = frozenset(name for _, name, _ in DASHBOARD_EVENT_ROWS)

METRIC_EVENT_COUNTS = (
    "Distinct analytics_events rows grouped by event_name within the selected UTC "
    f"date range on occurred_at (inclusive bounds). Browser events are non-authoritative; "
    "server events (Lead Persisted, Checkout Opened, Payment Completed) are authoritative "
    "for funnel conversion truth. Duplicates are prevented at ingest via idempotency_key."
)
METRIC_CONVERSION_RATES = (
    "Each rate is numerator ÷ denominator × 100 within the same UTC date window on "
    "occurred_at. Denominator zero yields no rate (—). Server events are used for "
    "steps 5–7; browser events are engagement-only for earlier steps."
)
METRIC_ATTRIBUTION = (
    "Aggregated counts from allowlisted UTM keys in analytics_events.attribution "
    f"({', '.join(sorted(UTM_ATTRIBUTION_KEYS))}). No per-session browsing trails."
)
METRIC_CASE_STUDY_ENGAGEMENT = (
    "Aggregated Case Study Viewed counts grouped by server-known case_study_slug "
    "property — no individual visitor history."
)
METRIC_INSIGHT_ENGAGEMENT = (
    "Aggregated Insight Viewed counts grouped by server-known article_slug property "
    "— no individual visitor history."
)

CONVERSION_RATE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "landing_to_brief",
        "Landing → brief view",
        EVENT_BRIEF_VIEWED,
        EVENT_LANDING_VIEWED,
    ),
    (
        "brief_to_form",
        "Brief view → form start",
        EVENT_BRIEF_FORM_STARTED,
        EVENT_BRIEF_VIEWED,
    ),
    (
        "form_to_lead",
        "Form start → lead persisted",
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
    (
        "form_to_payment",
        "Form start → payment completed",
        EVENT_PAYMENT_COMPLETED,
        EVENT_BRIEF_FORM_STARTED,
    ),
)


@dataclass(frozen=True)
class AnalyticsDateRange:
    """Bounded UTC window for dashboard queries."""

    start: datetime
    end: datetime
    date_from: date
    date_to: date
    date_from_raw: str
    date_to_raw: str


@dataclass(frozen=True)
class EventCountRow:
    label: str
    event_name: str
    count: int
    authoritative: bool


@dataclass(frozen=True)
class ConversionRateRow:
    key: str
    label: str
    numerator: int
    denominator: int
    rate_pct: float | None
    numerator_event: str
    denominator_event: str


@dataclass(frozen=True)
class AttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    event_count: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    view_count: int


@dataclass(frozen=True)
class MarketingAnalyticsDashboardData:
    date_range: AnalyticsDateRange
    event_counts: tuple[EventCountRow, ...]
    conversion_rates: tuple[ConversionRateRow, ...]
    attribution: tuple[AttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    insight_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(default_factory=lambda: {
        "event_counts": METRIC_EVENT_COUNTS,
        "conversion_rates": METRIC_CONVERSION_RATES,
        "attribution": METRIC_ATTRIBUTION,
        "case_study_engagement": METRIC_CASE_STUDY_ENGAGEMENT,
        "insight_engagement": METRIC_INSIGHT_ENGAGEMENT,
    })


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


def parse_analytics_date_range(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse and bound date range params; default last 7 days in UTC."""
    reference = now or datetime.now(timezone.utc)
    today = reference.date()
    parsed_to = _parse_date_param(date_to) or today
    parsed_from = _parse_date_param(date_from) or (
        parsed_to - timedelta(days=DEFAULT_DATE_RANGE_DAYS - 1)
    )
    if parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from
    if (parsed_to - parsed_from).days + 1 > MAX_DATE_RANGE_DAYS:
        parsed_from = parsed_to - timedelta(days=MAX_DATE_RANGE_DAYS - 1)
    start = datetime.combine(parsed_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(parsed_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return AnalyticsDateRange(
        start=start,
        end=end,
        date_from=parsed_from,
        date_to=parsed_to,
        date_from_raw=parsed_from.isoformat(),
        date_to_raw=parsed_to.isoformat(),
    )


def _format_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def compute_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRateRow, ...]:
    """Build conversion rate rows from event name → count map."""
    rows: list[ConversionRateRow] = []
    for key, label, num_event, den_event in CONVERSION_RATE_SPECS:
        numerator = counts.get(num_event, 0)
        denominator = counts.get(den_event, 0)
        rows.append(
            ConversionRateRow(
                key=key,
                label=label,
                numerator=numerator,
                denominator=denominator,
                rate_pct=_format_rate(numerator, denominator),
                numerator_event=num_event,
                denominator_event=den_event,
            )
        )
    return tuple(rows)


def _build_event_counts(counts: dict[str, int]) -> tuple[EventCountRow, ...]:
    return tuple(
        EventCountRow(
            label=label,
            event_name=event_name,
            count=counts.get(event_name, 0),
            authoritative=authoritative,
        )
        for label, event_name, authoritative in DASHBOARD_EVENT_ROWS
    )


def load_marketing_analytics_dashboard(
    conn: psycopg.Connection,
    repo: MarketingAnalyticsRepository,
    *,
    date_range: AnalyticsDateRange,
    now: datetime | None = None,
    list_limit: int = DEFAULT_LIST_LIMIT,
) -> MarketingAnalyticsDashboardData:
    """Load marketing analytics aggregates for the selected date range."""
    raw_counts = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=tuple(DASHBOARD_EVENT_NAMES),
    )
    counts = dict(raw_counts)
    attribution_rows = repo.list_attribution_breakdown(
        conn,
        start=date_range.start,
        end=date_range.end,
        limit=list_limit,
    )
    case_study_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=list_limit,
    )
    insight_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_INSIGHT_VIEWED,
        slug_property="article_slug",
        limit=list_limit,
    )
    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        event_counts=_build_event_counts(counts),
        conversion_rates=compute_conversion_rates(counts),
        attribution=tuple(
            AttributionRow(
                utm_source=str(row["utm_source"]),
                utm_medium=str(row["utm_medium"]),
                utm_campaign=str(row["utm_campaign"]),
                event_count=int(row["event_count"]),
            )
            for row in attribution_rows
        ),
        case_study_engagement=tuple(
            ContentEngagementRow(slug=str(row["slug"]), view_count=int(row["view_count"]))
            for row in case_study_rows
        ),
        insight_engagement=tuple(
            ContentEngagementRow(slug=str(row["slug"]), view_count=int(row["view_count"]))
            for row in insight_rows
        ),
        generated_at=now or datetime.now(timezone.utc),
    )


def empty_dashboard_data(date_range: AnalyticsDateRange) -> MarketingAnalyticsDashboardData:
    """Zeroed dashboard shell for missing DB or empty ranges."""
    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        event_counts=_build_event_counts({}),
        conversion_rates=compute_conversion_rates({}),
        attribution=(),
        case_study_engagement=(),
        insight_engagement=(),
        generated_at=datetime.now(timezone.utc),
    )


def dashboard_has_data(data: MarketingAnalyticsDashboardData) -> bool:
    """True when any section has non-zero aggregates."""
    if any(row.count > 0 for row in data.event_counts):
        return True
    if data.attribution:
        return True
    if data.case_study_engagement or data.insight_engagement:
        return True
    return False


def render_analytics_export_csv(data: MarketingAnalyticsDashboardData) -> str:
    """CSV export with aggregated results only."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    dr = data.date_range
    writer.writerow(["Marketing analytics export"])
    writer.writerow(["date_from", dr.date_from_raw])
    writer.writerow(["date_to", dr.date_to_raw])
    writer.writerow(["timezone", DASHBOARD_TIMEZONE])
    writer.writerow([])
    writer.writerow(["Event counts"])
    writer.writerow(["label", "event_name", "source", "count"])
    for row in data.event_counts:
        source = "server" if row.authoritative else "browser"
        writer.writerow([row.label, row.event_name, source, row.count])
    writer.writerow([])
    writer.writerow(["Conversion rates"])
    writer.writerow(
        ["label", "numerator_event", "numerator", "denominator_event", "denominator", "rate_pct"]
    )
    for row in data.conversion_rates:
        rate = "" if row.rate_pct is None else row.rate_pct
        writer.writerow(
            [
                row.label,
                row.numerator_event,
                row.numerator,
                row.denominator_event,
                row.denominator,
                rate,
            ]
        )
    writer.writerow([])
    writer.writerow(["Attribution"])
    writer.writerow(["utm_source", "utm_medium", "utm_campaign", "event_count"])
    for row in data.attribution:
        writer.writerow([row.utm_source, row.utm_medium, row.utm_campaign, row.event_count])
    writer.writerow([])
    writer.writerow(["Case study engagement"])
    writer.writerow(["slug", "view_count"])
    for row in data.case_study_engagement:
        writer.writerow([row.slug, row.view_count])
    writer.writerow([])
    writer.writerow(["Insight engagement"])
    writer.writerow(["slug", "view_count"])
    for row in data.insight_engagement:
        writer.writerow([row.slug, row.view_count])
    return buffer.getvalue()
