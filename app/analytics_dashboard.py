"""First-party marketing analytics dashboard — explicit metric definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

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
)
from app.repositories.protocols import AnalyticsDashboardRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_RANGE_DAYS = 7
ALLOWED_RANGE_PRESETS = (7, 30, 90)
MAX_RANGE_DAYS = 90
MAX_ATTRIBUTION_ROWS = 25
MAX_CONTENT_ROWS = 25

METRIC_EVENT_VOLUME = (
    "Count of stored analytics_events rows whose occurred_at falls in the selected "
    f"UTC window [{DASHBOARD_TIMEZONE}]. Browser events are client-reported page "
    "engagement; server events are authoritative conversion signals recorded after "
    "CRM/Stripe actions. Duplicate idempotency keys and bot user agents are rejected "
    "at ingest and are not counted."
)
METRIC_CRM_LEADS = (
    "Count of project_briefs rows with created_at in the UTC window — authoritative "
    "lead intake regardless of client-side form-start events."
)
METRIC_CRM_CHECKOUTS = (
    "Count of project_briefs rows with stripe_session_id set and created_at in the "
    "UTC window — authoritative checkout opens."
)
METRIC_CRM_PAYMENTS = (
    "Count of project_briefs rows with status paid and paid_at in the UTC window — "
    "authoritative paid diagnostic completions (server/webhook truth)."
)
METRIC_ATTRIBUTION = (
    "Aggregated event counts grouped by allowlisted utm_source, utm_medium, or "
    "utm_campaign from analytics_events.attribution JSONB. Individual sessions and "
    "paths are not listed."
)
METRIC_CONTENT = (
    "Aggregated view counts grouped by server-known case_study_slug or article_slug "
    "properties — no per-visitor browsing history."
)

EventSource = Literal["browser", "server"]
EventCategory = Literal["engagement", "conversion"]

DASHBOARD_EVENT_ROWS: tuple[tuple[str, EventSource, EventCategory], ...] = (
    (EVENT_LANDING_VIEWED, "browser", "engagement"),
    (EVENT_SERVICES_VIEWED, "browser", "engagement"),
    (EVENT_CASE_STUDIES_VIEWED, "browser", "engagement"),
    (EVENT_INSIGHTS_VIEWED, "browser", "engagement"),
    (EVENT_BRIEF_VIEWED, "browser", "conversion"),
    (EVENT_BRIEF_FORM_STARTED, "browser", "conversion"),
    (EVENT_CONTACT_INITIATED, "browser", "conversion"),
    (EVENT_LEAD_PERSISTED, "server", "conversion"),
    (EVENT_CHECKOUT_OPENED, "server", "conversion"),
    (EVENT_PAYMENT_COMPLETED, "server", "conversion"),
)


@dataclass(frozen=True)
class AnalyticsDateRange:
    start: datetime
    end: datetime
    preset_days: int | None = None

    @property
    def label(self) -> str:
        if self.preset_days is not None:
            return f"Last {self.preset_days} days"
        start_day = self.start.astimezone(ZoneInfo(DASHBOARD_TIMEZONE)).date()
        end_day = (self.end - timedelta(microseconds=1)).astimezone(
            ZoneInfo(DASHBOARD_TIMEZONE)
        ).date()
        return f"{start_day.isoformat()} – {end_day.isoformat()} UTC"


@dataclass(frozen=True)
class FunnelEventCount:
    event_name: str
    count: int
    source: EventSource
    category: EventCategory


@dataclass(frozen=True)
class CrmFunnelCounts:
    leads: int
    checkouts: int
    payments: int


@dataclass(frozen=True)
class ConversionRate:
    label: str
    numerator: int
    denominator: int
    numerator_definition: str
    denominator_definition: str
    source: Literal["browser", "server", "mixed"]

    @property
    def rate_percent(self) -> float | None:
        return format_conversion_rate(self.numerator, self.denominator)


@dataclass(frozen=True)
class AttributionBucket:
    dimension: str
    key: str
    event_count: int
    lead_count: int


@dataclass(frozen=True)
class ContentEngagementRow:
    content_type: Literal["case_study", "article"]
    slug: str
    views: int


@dataclass(frozen=True)
class AnalyticsDashboardData:
    date_range: AnalyticsDateRange
    event_counts: tuple[FunnelEventCount, ...]
    crm_counts: CrmFunnelCounts
    conversion_rates: tuple[ConversionRate, ...]
    attribution: tuple[AttributionBucket, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "event_volume": METRIC_EVENT_VOLUME,
            "crm_leads": METRIC_CRM_LEADS,
            "crm_checkouts": METRIC_CRM_CHECKOUTS,
            "crm_payments": METRIC_CRM_PAYMENTS,
            "attribution": METRIC_ATTRIBUTION,
            "content": METRIC_CONTENT,
        }
    )


def format_conversion_rate(numerator: int, denominator: int) -> float | None:
    """Return percentage rounded to one decimal, or None when denominator is zero."""
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def parse_analytics_date_range(
    *,
    days: str | None = None,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse a bounded UTC date range from query parameters."""
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    if start or end:
        if not start or not end:
            raise ValueError("Both start and end are required for a custom range.")
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        if end_day < start_day:
            raise ValueError("end must be on or after start.")
        range_start = _utc_midnight(start_day)
        range_end = _utc_midnight(end_day + timedelta(days=1))
        span_days = (range_end - range_start).days
        if span_days > MAX_RANGE_DAYS:
            raise ValueError(f"Custom range cannot exceed {MAX_RANGE_DAYS} days.")
        if span_days < 1:
            raise ValueError("Range must include at least one day.")
        return AnalyticsDateRange(start=range_start, end=range_end, preset_days=None)

    preset = DEFAULT_RANGE_DAYS
    if days:
        try:
            preset = int(days)
        except ValueError as exc:
            raise ValueError("days must be an integer preset.") from exc
    if preset not in ALLOWED_RANGE_PRESETS:
        raise ValueError(f"days must be one of {ALLOWED_RANGE_PRESETS}.")
    range_end = reference.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    range_start = range_end - timedelta(days=preset)
    return AnalyticsDateRange(start=range_start, end=range_end, preset_days=preset)


def _event_count_map(counts: dict[str, int], event_name: str) -> int:
    return int(counts.get(event_name, 0))


def build_conversion_rates(
    *,
    event_counts: dict[str, int],
    crm_counts: CrmFunnelCounts,
) -> tuple[ConversionRate, ...]:
    landing = _event_count_map(event_counts, EVENT_LANDING_VIEWED)
    brief_viewed = _event_count_map(event_counts, EVENT_BRIEF_VIEWED)
    form_started = _event_count_map(event_counts, EVENT_BRIEF_FORM_STARTED)
    lead_persisted = _event_count_map(event_counts, EVENT_LEAD_PERSISTED)
    checkout_opened = _event_count_map(event_counts, EVENT_CHECKOUT_OPENED)
    payment_completed = _event_count_map(event_counts, EVENT_PAYMENT_COMPLETED)

    return (
        ConversionRate(
            label="Landing → Brief view",
            numerator=brief_viewed,
            denominator=landing,
            numerator_definition=f"Count of `{EVENT_BRIEF_VIEWED}` events",
            denominator_definition=f"Count of `{EVENT_LANDING_VIEWED}` events",
            source="browser",
        ),
        ConversionRate(
            label="Brief view → Form start",
            numerator=form_started,
            denominator=brief_viewed,
            numerator_definition=f"Count of `{EVENT_BRIEF_FORM_STARTED}` events",
            denominator_definition=f"Count of `{EVENT_BRIEF_VIEWED}` events",
            source="browser",
        ),
        ConversionRate(
            label="Form start → Lead persisted",
            numerator=lead_persisted,
            denominator=form_started,
            numerator_definition=f"Count of server `{EVENT_LEAD_PERSISTED}` events",
            denominator_definition=f"Count of `{EVENT_BRIEF_FORM_STARTED}` browser events",
            source="mixed",
        ),
        ConversionRate(
            label="Lead persisted → Checkout opened",
            numerator=checkout_opened,
            denominator=lead_persisted,
            numerator_definition=f"Count of server `{EVENT_CHECKOUT_OPENED}` events",
            denominator_definition=f"Count of server `{EVENT_LEAD_PERSISTED}` events",
            source="server",
        ),
        ConversionRate(
            label="Checkout opened → Payment completed",
            numerator=payment_completed,
            denominator=checkout_opened,
            numerator_definition=f"Count of server `{EVENT_PAYMENT_COMPLETED}` events",
            denominator_definition=f"Count of server `{EVENT_CHECKOUT_OPENED}` events",
            source="server",
        ),
        ConversionRate(
            label="Lead → Paid diagnostic (CRM)",
            numerator=crm_counts.payments,
            denominator=crm_counts.leads,
            numerator_definition="project_briefs with status paid and paid_at in range",
            denominator_definition="project_briefs with created_at in range",
            source="server",
        ),
    )


def load_analytics_dashboard(
    conn: psycopg.Connection,
    repo: AnalyticsDashboardRepository,
    *,
    date_range: AnalyticsDateRange,
    now: datetime | None = None,
) -> AnalyticsDashboardData:
    """Load aggregated analytics dashboard data for the selected UTC window."""
    reference = now or datetime.now(timezone.utc)
    event_names = [name for name, _, _ in DASHBOARD_EVENT_ROWS]
    raw_counts = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=event_names,
    )
    event_counts = tuple(
        FunnelEventCount(
            event_name=event_name,
            count=_event_count_map(raw_counts, event_name),
            source=source,
            category=category,
        )
        for event_name, source, category in DASHBOARD_EVENT_ROWS
    )
    crm_counts = CrmFunnelCounts(
        **repo.count_crm_funnel(
            conn,
            start=date_range.start,
            end=date_range.end,
        )
    )
    attribution_rows: list[AttributionBucket] = []
    for dimension in ("utm_source", "utm_medium", "utm_campaign"):
        for row in repo.list_attribution_buckets(
            conn,
            start=date_range.start,
            end=date_range.end,
            dimension=dimension,
            limit=MAX_ATTRIBUTION_ROWS,
        ):
            attribution_rows.append(
                AttributionBucket(
                    dimension=dimension,
                    key=str(row["key"]),
                    event_count=int(row["event_count"]),
                    lead_count=int(row["lead_count"]),
                )
            )
    case_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=MAX_CONTENT_ROWS,
    )
    article_rows = repo.list_content_engagement(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_name=EVENT_INSIGHT_VIEWED,
        slug_property="article_slug",
        limit=MAX_CONTENT_ROWS,
    )
    return AnalyticsDashboardData(
        date_range=date_range,
        event_counts=event_counts,
        crm_counts=crm_counts,
        conversion_rates=build_conversion_rates(
            event_counts=raw_counts,
            crm_counts=crm_counts,
        ),
        attribution=tuple(attribution_rows),
        case_study_engagement=tuple(
            ContentEngagementRow(content_type="case_study", slug=str(row["slug"]), views=int(row["views"]))
            for row in case_rows
        ),
        article_engagement=tuple(
            ContentEngagementRow(content_type="article", slug=str(row["slug"]), views=int(row["views"]))
            for row in article_rows
        ),
        generated_at=reference,
    )
