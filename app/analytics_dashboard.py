"""Marketing analytics dashboard — funnel, attribution, and content engagement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
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
MAX_DATE_RANGE_DAYS = 90
DEFAULT_PERIOD_DAYS = 7
ALLOWED_PERIOD_DAYS = frozenset({7, 30, 90})
ATTRIBUTION_ROW_LIMIT = 20
CONTENT_SLUG_LIMIT = 15

_PERIOD_PATTERN = re.compile(r"^(\d+)d$", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DASHBOARD_EVENT_NAMES: tuple[str, ...] = (
    EVENT_LANDING_VIEWED,
    EVENT_SERVICES_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_CASE_STUDY_VIEWED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_INSIGHT_VIEWED,
    EVENT_BRIEF_VIEWED,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    EVENT_CONTACT_INITIATED,
)

AUTHORITATIVE_EVENT_NAMES = frozenset(
    {
        EVENT_LEAD_PERSISTED,
        EVENT_CHECKOUT_OPENED,
        EVENT_PAYMENT_COMPLETED,
    }
)

METRIC_ENGAGEMENT_EVENTS = (
    "Distinct analytics_events rows in the selected UTC window. "
    "Browser-ingested engagement events only; bots rejected at ingest; "
    "duplicates deduplicated by idempotency_key."
)
METRIC_CONVERSION_EVENTS = (
    "Distinct analytics_events rows for server-authoritative conversion events "
    "(Lead Persisted, Checkout Opened, Payment Completed). "
    "Counted by occurred_at in UTC; late-arriving events appear in the window "
    "when they occurred, not when received."
)
METRIC_ATTRIBUTION = (
    "Aggregated event counts grouped by allowlisted UTM keys "
    "(utm_source, utm_medium, utm_campaign) from analytics_events.attribution. "
    "Empty values shown as (direct) / (none). No individual session identifiers."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Aggregated view counts by server-known slug (case_study_slug or article_slug). "
    "No per-visitor browsing history."
)
METRIC_CONVERSION_RATES = (
    "Ratio of numerator event count to denominator event count in the same UTC window. "
    "Shows em dash when denominator is zero. Server events marked authoritative."
)


@dataclass(frozen=True)
class EventCountRow:
    event_name: str
    count: int
    source: Literal["browser", "server"]


@dataclass(frozen=True)
class ConversionRateRow:
    key: str
    label: str
    numerator: int
    denominator: int
    rate_pct: float | None
    numerator_definition: str
    denominator_definition: str


@dataclass(frozen=True)
class AttributionRow:
    source: str
    medium: str
    campaign: str
    event_count: int
    lead_count: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    content_type: Literal["case_study", "article"]
    views: int


@dataclass(frozen=True)
class AnalyticsDateRange:
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True)
class AnalyticsDashboardData:
    date_range: AnalyticsDateRange
    engagement_events: tuple[EventCountRow, ...]
    conversion_events: tuple[EventCountRow, ...]
    conversion_rates: tuple[ConversionRateRow, ...]
    attribution_rows: tuple[AttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "engagement_events": METRIC_ENGAGEMENT_EVENTS,
            "conversion_events": METRIC_CONVERSION_EVENTS,
            "conversion_rates": METRIC_CONVERSION_RATES,
            "attribution": METRIC_ATTRIBUTION,
            "content_engagement": METRIC_CONTENT_ENGAGEMENT,
        }
    )


def _utc_midnight(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _validate_bounded_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ValueError("end must be after start")
    if (end - start).days > MAX_DATE_RANGE_DAYS:
        raise ValueError(f"date range cannot exceed {MAX_DATE_RANGE_DAYS} days")


def parse_analytics_date_range(
    *,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    reference: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse a bounded UTC date range from query parameters."""
    now = reference or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if period and (start or end):
        raise ValueError("use either period or start/end, not both")

    if start or end:
        if not start or not end:
            raise ValueError("start and end are both required")
        if not _DATE_PATTERN.match(start) or not _DATE_PATTERN.match(end):
            raise ValueError("start and end must be YYYY-MM-DD")
        start_dt = _utc_midnight(date.fromisoformat(start))
        end_dt = _utc_midnight(date.fromisoformat(end)) + timedelta(days=1)
        _validate_bounded_range(start_dt, end_dt)
        label = f"{start} – {end} UTC"
        return AnalyticsDateRange(start=start_dt, end=end_dt, label=label)

    days = DEFAULT_PERIOD_DAYS
    if period:
        match = _PERIOD_PATTERN.match(period.strip())
        if not match:
            raise ValueError("period must look like 7d, 30d, or 90d")
        days = int(match.group(1))
        if days not in ALLOWED_PERIOD_DAYS:
            raise ValueError(f"period must be one of {sorted(ALLOWED_PERIOD_DAYS)}")

    start_dt = now - timedelta(days=days)
    return AnalyticsDateRange(
        start=start_dt,
        end=now,
        label=f"Last {days} days (UTC)",
    )


def _event_source(event_name: str) -> Literal["browser", "server"]:
    if event_name in AUTHORITATIVE_EVENT_NAMES:
        return "server"
    return "browser"


def _compute_rate(
    *,
    key: str,
    label: str,
    numerator_name: str,
    denominator_name: str,
    counts: dict[str, int],
    numerator_definition: str,
    denominator_definition: str,
) -> ConversionRateRow:
    numerator = counts.get(numerator_name, 0)
    denominator = counts.get(denominator_name, 0)
    rate_pct = round(100.0 * numerator / denominator, 1) if denominator > 0 else None
    return ConversionRateRow(
        key=key,
        label=label,
        numerator=numerator,
        denominator=denominator,
        rate_pct=rate_pct,
        numerator_definition=numerator_definition,
        denominator_definition=denominator_definition,
    )


def _build_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRateRow, ...]:
    return (
        _compute_rate(
            key="landing_to_brief",
            label="Landing → brief view",
            numerator_name=EVENT_BRIEF_VIEWED,
            denominator_name=EVENT_LANDING_VIEWED,
            counts=counts,
            numerator_definition="Brief Viewed events (browser)",
            denominator_definition="Landing Viewed events (browser)",
        ),
        _compute_rate(
            key="brief_to_form",
            label="Brief view → form start",
            numerator_name=EVENT_BRIEF_FORM_STARTED,
            denominator_name=EVENT_BRIEF_VIEWED,
            counts=counts,
            numerator_definition="Brief Form Started events (browser)",
            denominator_definition="Brief Viewed events (browser)",
        ),
        _compute_rate(
            key="form_to_lead",
            label="Form start → lead persisted",
            numerator_name=EVENT_LEAD_PERSISTED,
            denominator_name=EVENT_BRIEF_FORM_STARTED,
            counts=counts,
            numerator_definition="Lead Persisted events (server, authoritative)",
            denominator_definition="Brief Form Started events (browser)",
        ),
        _compute_rate(
            key="lead_to_checkout",
            label="Lead → checkout opened",
            numerator_name=EVENT_CHECKOUT_OPENED,
            denominator_name=EVENT_LEAD_PERSISTED,
            counts=counts,
            numerator_definition="Checkout Opened events (server, authoritative)",
            denominator_definition="Lead Persisted events (server, authoritative)",
        ),
        _compute_rate(
            key="checkout_to_payment",
            label="Checkout → payment completed",
            numerator_name=EVENT_PAYMENT_COMPLETED,
            denominator_name=EVENT_CHECKOUT_OPENED,
            counts=counts,
            numerator_definition="Payment Completed events (server, authoritative)",
            denominator_definition="Checkout Opened events (server, authoritative)",
        ),
        _compute_rate(
            key="landing_to_payment",
            label="Landing → payment completed",
            numerator_name=EVENT_PAYMENT_COMPLETED,
            denominator_name=EVENT_LANDING_VIEWED,
            counts=counts,
            numerator_definition="Payment Completed events (server, authoritative)",
            denominator_definition="Landing Viewed events (browser)",
        ),
    )


def _normalize_attribution_label(value: str | None, *, empty_label: str) -> str:
    if not value or not str(value).strip():
        return empty_label
    return str(value).strip()


def load_analytics_dashboard(
    conn: psycopg.Connection,
    repo: AnalyticsDashboardRepository,
    *,
    date_range: AnalyticsDateRange,
    generated_at: datetime | None = None,
) -> AnalyticsDashboardData:
    """Load marketing analytics aggregates for the selected UTC window."""
    raw_counts = repo.count_events_in_range(
        conn,
        period_start=date_range.start,
        period_end=date_range.end,
        event_names=DASHBOARD_EVENT_NAMES,
    )
    counts = {name: total for name, total in raw_counts}

    engagement_order = (
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
    conversion_order = (
        EVENT_LEAD_PERSISTED,
        EVENT_CHECKOUT_OPENED,
        EVENT_PAYMENT_COMPLETED,
    )

    engagement_events = tuple(
        EventCountRow(
            event_name=name,
            count=counts.get(name, 0),
            source=_event_source(name),
        )
        for name in engagement_order
    )
    conversion_events = tuple(
        EventCountRow(
            event_name=name,
            count=counts.get(name, 0),
            source="server",
        )
        for name in conversion_order
    )

    attribution_raw = repo.count_attribution_in_range(
        conn,
        period_start=date_range.start,
        period_end=date_range.end,
        limit=ATTRIBUTION_ROW_LIMIT,
    )
    leads_by_source = {
        source: total
        for source, total in repo.count_leads_by_utm_source(
            conn,
            period_start=date_range.start,
            period_end=date_range.end,
            limit=ATTRIBUTION_ROW_LIMIT,
        )
    }
    attribution_rows = tuple(
        AttributionRow(
            source=_normalize_attribution_label(row.get("source"), empty_label="(direct)"),
            medium=_normalize_attribution_label(row.get("medium"), empty_label="(none)"),
            campaign=_normalize_attribution_label(row.get("campaign"), empty_label="(none)"),
            event_count=int(row.get("event_count") or 0),
            lead_count=leads_by_source.get(
                _normalize_attribution_label(row.get("source"), empty_label="(direct)"),
                0,
            ),
        )
        for row in attribution_raw
    )

    case_study_engagement = tuple(
        ContentEngagementRow(slug=slug, content_type="case_study", views=views)
        for slug, views in repo.count_content_engagement(
            conn,
            period_start=date_range.start,
            period_end=date_range.end,
            event_name=EVENT_CASE_STUDY_VIEWED,
            slug_property="case_study_slug",
            limit=CONTENT_SLUG_LIMIT,
        )
    )
    article_engagement = tuple(
        ContentEngagementRow(slug=slug, content_type="article", views=views)
        for slug, views in repo.count_content_engagement(
            conn,
            period_start=date_range.start,
            period_end=date_range.end,
            event_name=EVENT_INSIGHT_VIEWED,
            slug_property="article_slug",
            limit=CONTENT_SLUG_LIMIT,
        )
    )

    return AnalyticsDashboardData(
        date_range=date_range,
        engagement_events=engagement_events,
        conversion_events=conversion_events,
        conversion_rates=_build_conversion_rates(counts),
        attribution_rows=attribution_rows,
        case_study_engagement=case_study_engagement,
        article_engagement=article_engagement,
        generated_at=generated_at or datetime.now(timezone.utc),
    )


def render_analytics_export_csv(data: AnalyticsDashboardData) -> str:
    """Render aggregated analytics dashboard rows as CSV (no session-level data)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "metric", "value", "numerator", "denominator", "definition"])

    writer.writerow(["meta", "period", data.date_range.label, "", "", ""])
    writer.writerow(
        [
            "meta",
            "generated_at",
            data.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "",
            "",
            "",
        ]
    )

    for row in data.engagement_events:
        writer.writerow(
            [
                "engagement",
                row.event_name,
                row.count,
                "",
                "",
                data.metric_definitions["engagement_events"],
            ]
        )
    for row in data.conversion_events:
        writer.writerow(
            [
                "conversion",
                row.event_name,
                row.count,
                "",
                "",
                data.metric_definitions["conversion_events"],
            ]
        )
    for row in data.conversion_rates:
        writer.writerow(
            [
                "conversion_rate",
                row.label,
                "" if row.rate_pct is None else row.rate_pct,
                row.numerator,
                row.denominator,
                f"{row.numerator_definition} / {row.denominator_definition}",
            ]
        )
    for row in data.attribution_rows:
        writer.writerow(
            [
                "attribution",
                f"{row.source}|{row.medium}|{row.campaign}",
                row.event_count,
                row.lead_count,
                "",
                data.metric_definitions["attribution"],
            ]
        )
    for row in data.case_study_engagement:
        writer.writerow(
            [
                "case_study",
                row.slug,
                row.views,
                "",
                "",
                data.metric_definitions["content_engagement"],
            ]
        )
    for row in data.article_engagement:
        writer.writerow(
            [
                "article",
                row.slug,
                row.views,
                "",
                "",
                data.metric_definitions["content_engagement"],
            ]
        )
    return buffer.getvalue()
