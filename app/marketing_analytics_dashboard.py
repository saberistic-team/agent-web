"""Marketing analytics dashboard — funnel, attribution, and content engagement."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

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
    UTM_ATTRIBUTION_KEYS,
)
from app.repositories.protocols import MarketingAnalyticsRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 90
DEFAULT_LIST_LIMIT = 20

VALID_PRESET_DAYS = frozenset({7, 30, 90})

BROWSER_ENGAGEMENT_EVENTS: tuple[str, ...] = (
    EVENT_LANDING_VIEWED,
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

METRIC_ENGAGEMENT_EVENTS = (
    "Count of distinct ingested browser events in analytics_events for the selected "
    f"UTC window (occurred_at >= start, occurred_at < end). Bots and duplicate "
    "idempotency keys are excluded at ingest; late events use client occurred_at."
)
METRIC_SERVER_EVENTS = (
    "Count of authoritative server events in analytics_events for the UTC window. "
    "These are the source of truth for leads, checkout, and payment steps."
)
METRIC_CONVERSION_RATE = (
    "Percentage = 100 × numerator ÷ denominator when denominator > 0; otherwise "
    "shown as em dash. Numerator and denominator definitions are listed per row."
)
METRIC_ATTRIBUTION = (
    "Aggregated counts grouped by allowlisted UTM keys (utm_source, utm_medium, "
    "utm_campaign). Empty values bucket as (direct) / (none). No per-session rows."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Aggregated view counts by server-known slug from analytics_events properties "
    "(case_study_slug or article_slug). Individual browsing histories are not shown."
)
METRIC_BRIEF_LEADS = (
    "Count of project_briefs rows with created_at in the UTC window (Postgres CRM)."
)
METRIC_BRIEF_PAYMENTS = (
    "Count of project_briefs with status paid and paid_at in the UTC window."
)

EventSource = Literal["browser", "server"]


@dataclass(frozen=True)
class AnalyticsDateRange:
    start: datetime
    end: datetime
    preset_days: int | None
    from_date: date | None
    to_date: date | None

    @property
    def label(self) -> str:
        if self.preset_days is not None:
            return f"Last {self.preset_days} days"
        if self.from_date and self.to_date:
            return f"{self.from_date.isoformat()} – {self.to_date.isoformat()}"
        return "Selected window"


@dataclass(frozen=True)
class EventCountRow:
    event_name: str
    count: int
    source: EventSource


@dataclass(frozen=True)
class ConversionRateRow:
    name: str
    numerator: int
    denominator: int
    rate_percent: float | None
    numerator_definition: str
    denominator_definition: str


@dataclass(frozen=True)
class AttributionRow:
    source: str
    medium: str
    campaign: str
    engagement_events: int
    leads: int
    payments: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    content_type: Literal["case_study", "article"]
    views: int


@dataclass(frozen=True)
class BriefFunnelCounts:
    leads: int
    checkouts_opened: int
    payments: int


@dataclass(frozen=True)
class MarketingAnalyticsDashboardData:
    date_range: AnalyticsDateRange
    engagement_events: tuple[EventCountRow, ...]
    server_events: tuple[EventCountRow, ...]
    brief_funnel: BriefFunnelCounts
    conversion_rates: tuple[ConversionRateRow, ...]
    attribution: tuple[AttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "engagement_events": METRIC_ENGAGEMENT_EVENTS,
            "server_events": METRIC_SERVER_EVENTS,
            "conversion_rate": METRIC_CONVERSION_RATE,
            "attribution": METRIC_ATTRIBUTION,
            "content_engagement": METRIC_CONTENT_ENGAGEMENT,
            "brief_leads": METRIC_BRIEF_LEADS,
            "brief_payments": METRIC_BRIEF_PAYMENTS,
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


def _utc_start_of_day(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def parse_analytics_date_range(
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDateRange:
    """Resolve a bounded UTC analytics window (max MAX_RANGE_DAYS)."""
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)

    parsed_from = _parse_date_param(date_from)
    parsed_to = _parse_date_param(date_to)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from

    preset: int | None = None
    if parsed_from and parsed_to:
        start = _utc_start_of_day(parsed_from)
        end = _utc_start_of_day(parsed_to) + timedelta(days=1)
    else:
        preset = days if days in VALID_PRESET_DAYS else DEFAULT_RANGE_DAYS
        end = reference
        start = end - timedelta(days=preset)

    if (end - start).days > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS)
        preset = MAX_RANGE_DAYS

    if start >= end:
        start = end - timedelta(days=DEFAULT_RANGE_DAYS)
        preset = DEFAULT_RANGE_DAYS

    return AnalyticsDateRange(
        start=start,
        end=end,
        preset_days=preset if parsed_from is None or parsed_to is None else None,
        from_date=parsed_from,
        to_date=parsed_to,
    )


def conversion_rate_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _event_count_map(rows: list[tuple[str, int]]) -> dict[str, int]:
    return {name: count for name, count in rows}


def _build_conversion_rates(
    engagement: dict[str, int],
    server: dict[str, int],
    brief: BriefFunnelCounts,
) -> tuple[ConversionRateRow, ...]:
    brief_viewed = engagement.get(EVENT_BRIEF_VIEWED, 0)
    form_started = engagement.get(EVENT_BRIEF_FORM_STARTED, 0)
    lead_persisted = server.get(EVENT_LEAD_PERSISTED, brief.leads)
    checkout_opened = server.get(EVENT_CHECKOUT_OPENED, brief.checkouts_opened)
    payment_completed = server.get(EVENT_PAYMENT_COMPLETED, brief.payments)

    rows = [
        ConversionRateRow(
            name="Brief view → lead",
            numerator=lead_persisted,
            denominator=brief_viewed,
            rate_percent=conversion_rate_percent(lead_persisted, brief_viewed),
            numerator_definition=f"Server `{EVENT_LEAD_PERSISTED}` events (authoritative)",
            denominator_definition=f"Browser `{EVENT_BRIEF_VIEWED}` events (engagement)",
        ),
        ConversionRateRow(
            name="Form start → lead",
            numerator=lead_persisted,
            denominator=form_started,
            rate_percent=conversion_rate_percent(lead_persisted, form_started),
            numerator_definition=f"Server `{EVENT_LEAD_PERSISTED}` events (authoritative)",
            denominator_definition=f"Browser `{EVENT_BRIEF_FORM_STARTED}` events (engagement)",
        ),
        ConversionRateRow(
            name="Lead → checkout",
            numerator=checkout_opened,
            denominator=lead_persisted,
            rate_percent=conversion_rate_percent(checkout_opened, lead_persisted),
            numerator_definition=f"Server `{EVENT_CHECKOUT_OPENED}` events",
            denominator_definition=f"Server `{EVENT_LEAD_PERSISTED}` events",
        ),
        ConversionRateRow(
            name="Checkout → payment",
            numerator=payment_completed,
            denominator=checkout_opened,
            rate_percent=conversion_rate_percent(payment_completed, checkout_opened),
            numerator_definition=f"Server `{EVENT_PAYMENT_COMPLETED}` events",
            denominator_definition=f"Server `{EVENT_CHECKOUT_OPENED}` events",
        ),
        ConversionRateRow(
            name="Lead → payment (CRM)",
            numerator=brief.payments,
            denominator=brief.leads,
            rate_percent=conversion_rate_percent(brief.payments, brief.leads),
            numerator_definition="project_briefs with status paid and paid_at in window",
            denominator_definition="project_briefs with created_at in window",
        ),
    ]
    return tuple(rows)


def _ordered_event_rows(
    event_names: tuple[str, ...],
    counts: dict[str, int],
    source: EventSource,
) -> tuple[EventCountRow, ...]:
    return tuple(
        EventCountRow(event_name=name, count=counts.get(name, 0), source=source)
        for name in event_names
    )


def load_marketing_analytics_dashboard(
    conn: psycopg.Connection,
    repo: MarketingAnalyticsRepository,
    *,
    date_range: AnalyticsDateRange,
    now: datetime | None = None,
    list_limit: int = DEFAULT_LIST_LIMIT,
) -> MarketingAnalyticsDashboardData:
    """Load marketing analytics sections for the selected UTC window."""
    reference = now or datetime.now(timezone.utc)
    start = date_range.start
    end = date_range.end

    engagement_counts = _event_count_map(
        repo.count_events_by_name(
            conn,
            start=start,
            end=end,
            event_names=BROWSER_ENGAGEMENT_EVENTS,
        )
    )
    server_counts = _event_count_map(
        repo.count_events_by_name(
            conn,
            start=start,
            end=end,
            event_names=SERVER_CONVERSION_EVENTS,
        )
    )
    brief_funnel = BriefFunnelCounts(**repo.count_brief_funnel(conn, start=start, end=end))
    attribution_raw = repo.list_attribution_summary(
        conn,
        start=start,
        end=end,
        limit=list_limit,
    )
    case_studies = repo.list_content_engagement(
        conn,
        start=start,
        end=end,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=list_limit,
    )
    articles = repo.list_content_engagement(
        conn,
        start=start,
        end=end,
        event_name=EVENT_INSIGHT_VIEWED,
        slug_property="article_slug",
        limit=list_limit,
    )

    return MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=_ordered_event_rows(
            BROWSER_ENGAGEMENT_EVENTS,
            engagement_counts,
            "browser",
        ),
        server_events=_ordered_event_rows(
            SERVER_CONVERSION_EVENTS,
            server_counts,
            "server",
        ),
        brief_funnel=brief_funnel,
        conversion_rates=_build_conversion_rates(
            engagement_counts,
            server_counts,
            brief_funnel,
        ),
        attribution=tuple(
            AttributionRow(
                source=str(row["source"]),
                medium=str(row["medium"]),
                campaign=str(row["campaign"]),
                engagement_events=int(row["engagement_events"]),
                leads=int(row["leads"]),
                payments=int(row["payments"]),
            )
            for row in attribution_raw
        ),
        case_study_engagement=tuple(
            ContentEngagementRow(
                slug=str(row["slug"]),
                content_type="case_study",
                views=int(row["views"]),
            )
            for row in case_studies
        ),
        article_engagement=tuple(
            ContentEngagementRow(
                slug=str(row["slug"]),
                content_type="article",
                views=int(row["views"]),
            )
            for row in articles
        ),
        generated_at=reference,
    )


def dashboard_has_data(data: MarketingAnalyticsDashboardData) -> bool:
    """True when any section has non-zero counts."""
    if any(row.count for row in data.engagement_events):
        return True
    if any(row.count for row in data.server_events):
        return True
    if data.brief_funnel.leads or data.brief_funnel.payments:
        return True
    if data.attribution:
        return True
    if data.case_study_engagement or data.article_engagement:
        return True
    return False


def dashboard_to_csv(data: MarketingAnalyticsDashboardData) -> str:
    """Serialize aggregated dashboard metrics to CSV (no raw events or session IDs)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "section",
            "metric",
            "value",
            "numerator",
            "denominator",
            "definition",
        ]
    )
    period = data.date_range.label
    definitions = data.metric_definitions

    for row in data.engagement_events:
        writer.writerow(
            [
                "browser_engagement",
                row.event_name,
                row.count,
                "",
                "",
                f"{definitions['engagement_events']} Period: {period}.",
            ]
        )
    for row in data.server_events:
        writer.writerow(
            [
                "server_conversion",
                row.event_name,
                row.count,
                "",
                "",
                f"{definitions['server_events']} Period: {period}.",
            ]
        )
    writer.writerow(
        [
            "crm_funnel",
            "leads",
            data.brief_funnel.leads,
            "",
            "",
            definitions["brief_leads"],
        ]
    )
    writer.writerow(
        [
            "crm_funnel",
            "payments",
            data.brief_funnel.payments,
            "",
            "",
            definitions["brief_payments"],
        ]
    )
    for row in data.conversion_rates:
        rate = "" if row.rate_percent is None else row.rate_percent
        writer.writerow(
            [
                "conversion_rate",
                row.name,
                rate,
                row.numerator,
                row.denominator,
                f"{row.numerator_definition} / {row.denominator_definition}",
            ]
        )
    for row in data.attribution:
        writer.writerow(
            [
                "attribution",
                f"{row.source}/{row.medium}/{row.campaign}",
                row.engagement_events,
                row.leads,
                row.payments,
                definitions["attribution"],
            ]
        )
    for row in data.case_study_engagement:
        writer.writerow(
            [
                "content_engagement",
                f"case_study:{row.slug}",
                row.views,
                "",
                "",
                definitions["content_engagement"],
            ]
        )
    for row in data.article_engagement:
        writer.writerow(
            [
                "content_engagement",
                f"article:{row.slug}",
                row.views,
                "",
                "",
                definitions["content_engagement"],
            ]
        )
    return buffer.getvalue()


def utm_attribution_key_list() -> tuple[str, ...]:
    """Allowlisted attribution keys surfaced on the dashboard."""
    return tuple(sorted(UTM_ATTRIBUTION_KEYS))
