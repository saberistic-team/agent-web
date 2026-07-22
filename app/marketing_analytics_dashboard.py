"""Marketing funnel, attribution, and content analytics — explicit metric definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
)
from app.repositories.protocols import MarketingAnalyticsRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 90
ATTRIBUTION_ROW_LIMIT = 50
CONTENT_SLUG_LIMIT = 30

METRIC_EVENT_WINDOW = (
    f"Events counted when occurred_at falls in the selected inclusive UTC date range "
    f"({DASHBOARD_TIMEZONE}). Late-arriving rows use occurred_at, not received_at. "
    "Duplicates are prevented at ingest via idempotency_key; bots and DNT traffic are "
    "dropped before persistence."
)
METRIC_BROWSER_ENGAGEMENT = (
    "Non-authoritative browser engagement events from analytics_events. "
    "These measure interest only — not CRM or payment truth."
)
METRIC_SERVER_CONVERSION = (
    "Authoritative server conversion events from analytics_events "
    "(Lead Persisted, Checkout Opened, Payment Completed). "
    "These are the source of truth for funnel steps 5–7."
)
METRIC_ATTRIBUTION = (
    "Allowlisted utm_source, utm_medium, and utm_campaign only. "
    "Engagement counts come from analytics_events.attribution; "
    "leads and payments come from project_briefs UTM columns in the same window."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Aggregated view counts by slug — no per-visitor session history is shown."
)

BROWSER_ENGAGEMENT_EVENTS: tuple[str, ...] = (
    EVENT_LANDING_VIEWED,
    EVENT_SERVICES_VIEWED,
    EVENT_CASE_STUDIES_VIEWED,
    EVENT_INSIGHTS_VIEWED,
    EVENT_BRIEF_VIEWED,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CONTACT_INITIATED,
)

SERVER_CONVERSION_EVENTS: tuple[str, ...] = (
    EVENT_LEAD_PERSISTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_PAYMENT_COMPLETED,
)

CONTENT_EVENTS: tuple[tuple[str, str], ...] = (
    (EVENT_CASE_STUDY_VIEWED, "case_study_slug"),
    (EVENT_INSIGHT_VIEWED, "article_slug"),
)

EventSource = Literal["browser", "server"]


@dataclass(frozen=True)
class MarketingAnalyticsFilters:
    start: datetime
    end_exclusive: datetime
    start_date: date
    end_date: date
    start_raw: str
    end_raw: str


@dataclass(frozen=True)
class EventCountRow:
    event_name: str
    count: int
    source: EventSource


@dataclass(frozen=True)
class ConversionRateRow:
    label: str
    numerator: int
    denominator: int
    rate_pct: float | None
    numerator_definition: str
    denominator_definition: str


@dataclass(frozen=True)
class AttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    engagement_events: int
    leads: int
    payments: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    views: int


@dataclass(frozen=True)
class MarketingAnalyticsDashboardData:
    filters: MarketingAnalyticsFilters
    engagement_events: tuple[EventCountRow, ...]
    server_events: tuple[EventCountRow, ...]
    conversion_rates: tuple[ConversionRateRow, ...]
    attribution: tuple[AttributionRow, ...]
    case_study_views: tuple[ContentEngagementRow, ...]
    article_views: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "event_window": METRIC_EVENT_WINDOW,
            "browser_engagement": METRIC_BROWSER_ENGAGEMENT,
            "server_conversion": METRIC_SERVER_CONVERSION,
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


def _default_end_date(reference: datetime) -> date:
    return reference.astimezone(timezone.utc).date()


def normalize_filters(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    reference: datetime | None = None,
) -> MarketingAnalyticsFilters:
    """Validate and bound the analytics date range (UTC, inclusive calendar dates)."""
    now = reference or datetime.now(timezone.utc)
    end_date = _parse_date_param(date_to) or _default_end_date(now)
    start_date = _parse_date_param(date_from) or (end_date - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    span_days = (end_date - start_date).days + 1
    if span_days > MAX_RANGE_DAYS:
        start_date = end_date - timedelta(days=MAX_RANGE_DAYS - 1)
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return MarketingAnalyticsFilters(
        start=start,
        end_exclusive=end_exclusive,
        start_date=start_date,
        end_date=end_date,
        start_raw=start_date.isoformat(),
        end_raw=end_date.isoformat(),
    )


def compute_rate_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _event_count(rows: tuple[EventCountRow, ...], event_name: str) -> int:
    for row in rows:
        if row.event_name == event_name:
            return row.count
    return 0


def _build_conversion_rates(
    engagement: tuple[EventCountRow, ...],
    server: tuple[EventCountRow, ...],
) -> tuple[ConversionRateRow, ...]:
    landing = _event_count(engagement, EVENT_LANDING_VIEWED)
    brief_viewed = _event_count(engagement, EVENT_BRIEF_VIEWED)
    form_started = _event_count(engagement, EVENT_BRIEF_FORM_STARTED)
    leads = _event_count(server, EVENT_LEAD_PERSISTED)
    checkouts = _event_count(server, EVENT_CHECKOUT_OPENED)
    payments = _event_count(server, EVENT_PAYMENT_COMPLETED)

    specs: list[tuple[str, int, int, str, str]] = [
        (
            "Brief view → form start",
            form_started,
            brief_viewed,
            f"Count of `{EVENT_BRIEF_FORM_STARTED}` browser events",
            f"Count of `{EVENT_BRIEF_VIEWED}` browser events",
        ),
        (
            "Form start → lead",
            leads,
            form_started,
            f"Count of `{EVENT_LEAD_PERSISTED}` server events",
            f"Count of `{EVENT_BRIEF_FORM_STARTED}` browser events",
        ),
        (
            "Lead → checkout",
            checkouts,
            leads,
            f"Count of `{EVENT_CHECKOUT_OPENED}` server events",
            f"Count of `{EVENT_LEAD_PERSISTED}` server events",
        ),
        (
            "Checkout → payment",
            payments,
            checkouts,
            f"Count of `{EVENT_PAYMENT_COMPLETED}` server events",
            f"Count of `{EVENT_CHECKOUT_OPENED}` server events",
        ),
        (
            "Landing → lead",
            leads,
            landing,
            f"Count of `{EVENT_LEAD_PERSISTED}` server events",
            f"Count of `{EVENT_LANDING_VIEWED}` browser events",
        ),
    ]
    return tuple(
        ConversionRateRow(
            label=label,
            numerator=numerator,
            denominator=denominator,
            rate_pct=compute_rate_pct(numerator, denominator),
            numerator_definition=numerator_def,
            denominator_definition=denominator_def,
        )
        for label, numerator, denominator, numerator_def, denominator_def in specs
    )


def _merge_attribution(
    engagement_rows: list[dict[str, Any]],
    brief_rows: list[dict[str, Any]],
) -> tuple[AttributionRow, ...]:
    merged: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in engagement_rows:
        key = (
            str(row.get("utm_source") or "(direct)"),
            str(row.get("utm_medium") or "(none)"),
            str(row.get("utm_campaign") or "(none)"),
        )
        bucket = merged.setdefault(
            key,
            {"engagement_events": 0, "leads": 0, "payments": 0},
        )
        bucket["engagement_events"] += int(row.get("engagement_events") or 0)
    for row in brief_rows:
        key = (
            str(row.get("utm_source") or "(direct)"),
            str(row.get("utm_medium") or "(none)"),
            str(row.get("utm_campaign") or "(none)"),
        )
        bucket = merged.setdefault(
            key,
            {"engagement_events": 0, "leads": 0, "payments": 0},
        )
        bucket["leads"] += int(row.get("leads") or 0)
        bucket["payments"] += int(row.get("payments") or 0)
    ordered = sorted(
        merged.items(),
        key=lambda item: (
            -item[1]["leads"],
            -item[1]["engagement_events"],
            item[0],
        ),
    )[:ATTRIBUTION_ROW_LIMIT]
    return tuple(
        AttributionRow(
            utm_source=key[0],
            utm_medium=key[1],
            utm_campaign=key[2],
            engagement_events=values["engagement_events"],
            leads=values["leads"],
            payments=values["payments"],
        )
        for key, values in ordered
    )


def load_marketing_analytics_dashboard(
    conn: psycopg.Connection,
    repo: MarketingAnalyticsRepository,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    reference: datetime | None = None,
) -> MarketingAnalyticsDashboardData:
    """Load marketing analytics dashboard sections from the repository."""
    filters = normalize_filters(
        date_from=date_from,
        date_to=date_to,
        reference=reference,
    )
    generated = reference or datetime.now(timezone.utc)

    engagement_counts = repo.count_events_by_name(
        conn,
        start=filters.start,
        end_exclusive=filters.end_exclusive,
        event_names=BROWSER_ENGAGEMENT_EVENTS,
    )
    server_counts = repo.count_events_by_name(
        conn,
        start=filters.start,
        end_exclusive=filters.end_exclusive,
        event_names=SERVER_CONVERSION_EVENTS,
    )

    engagement = tuple(
        EventCountRow(event_name=name, count=count, source="browser")
        for name, count in engagement_counts
    )
    server = tuple(
        EventCountRow(event_name=name, count=count, source="server")
        for name, count in server_counts
    )

    case_study_rows = repo.count_content_views(
        conn,
        start=filters.start,
        end_exclusive=filters.end_exclusive,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=CONTENT_SLUG_LIMIT,
    )
    article_rows = repo.count_content_views(
        conn,
        start=filters.start,
        end_exclusive=filters.end_exclusive,
        event_name=EVENT_INSIGHT_VIEWED,
        slug_property="article_slug",
        limit=CONTENT_SLUG_LIMIT,
    )

    attribution = _merge_attribution(
        repo.count_engagement_attribution(
            conn,
            start=filters.start,
            end_exclusive=filters.end_exclusive,
            limit=ATTRIBUTION_ROW_LIMIT,
        ),
        repo.count_brief_attribution(
            conn,
            start=filters.start,
            end_exclusive=filters.end_exclusive,
            limit=ATTRIBUTION_ROW_LIMIT,
        ),
    )

    return MarketingAnalyticsDashboardData(
        filters=filters,
        engagement_events=engagement,
        server_events=server,
        conversion_rates=_build_conversion_rates(engagement, server),
        attribution=attribution,
        case_study_views=tuple(
            ContentEngagementRow(slug=slug, views=views) for slug, views in case_study_rows
        ),
        article_views=tuple(
            ContentEngagementRow(slug=slug, views=views) for slug, views in article_rows
        ),
        generated_at=generated,
    )
