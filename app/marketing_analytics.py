"""Marketing funnel, attribution, and content analytics — load helpers and CSV export."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from app.analytics_ingest import SERVER_ONLY_EVENTS
from app.crm_export import neutralize_csv_cell
from app.repositories.protocols import MarketingAnalyticsRepository

DASHBOARD_TIMEZONE = "UTC"
VALID_PERIOD_DAYS = frozenset({7, 30, 90})
DEFAULT_PERIOD_DAYS = 7
DEFAULT_LIST_LIMIT = 20
DEFAULT_CONTENT_LIMIT = 20

DASHBOARD_ENGAGEMENT_EVENTS = (
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

DASHBOARD_SERVER_EVENTS = (
    EVENT_LEAD_PERSISTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_PAYMENT_COMPLETED,
)

EventSource = Literal["browser", "server"]

METRIC_ENGAGEMENT_COUNTS = (
    "Distinct browser-ingested engagement events in analytics_events, counted by event_name "
    f"using occurred_at in [{DASHBOARD_TIMEZONE}] and idempotency_key deduplication at ingest. "
    "Bot user agents are rejected before persistence."
)
METRIC_SERVER_CONVERSIONS = (
    "Authoritative server conversion events in analytics_events (Lead Persisted, Checkout Opened, "
    "Payment Completed). Client-side Brief Success Viewed and Checkout Cancelled are excluded."
)
METRIC_ATTRIBUTION = (
    "Aggregated counts grouped by allowlisted UTM fields (utm_source, utm_medium, utm_campaign) "
    "from event attribution JSON and project_briefs columns. No session-level drill-down."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Aggregated page views by known internal slug (case_study_slug or article_slug). "
    "Counts distinct idempotency keys — no per-visitor browsing history."
)
METRIC_ABANDONED_CHECKOUTS = (
    "project_briefs with status pending_payment, stripe_session_id set, and created_at in range."
)

_EVENT_LABELS: dict[str, str] = {
    EVENT_LANDING_VIEWED: "Landing views",
    EVENT_SERVICES_VIEWED: "Services views",
    EVENT_CASE_STUDIES_VIEWED: "Case studies index",
    EVENT_CASE_STUDY_VIEWED: "Case study detail",
    EVENT_INSIGHTS_VIEWED: "Insights index",
    EVENT_INSIGHT_VIEWED: "Insight article",
    EVENT_BRIEF_VIEWED: "Brief page views",
    EVENT_BRIEF_FORM_STARTED: "Brief form starts",
    EVENT_CONTACT_INITIATED: "Contact initiated",
    EVENT_LEAD_PERSISTED: "Leads persisted",
    EVENT_CHECKOUT_OPENED: "Checkouts opened",
    EVENT_PAYMENT_COMPLETED: "Payments completed",
}


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
    content_type: Literal["case_study", "article"]
    slug: str
    views: int


@dataclass(frozen=True)
class MarketingAnalyticsData:
    period_days: int
    period_start: datetime
    period_end: datetime
    engagement_counts: tuple[EventCount, ...]
    server_conversion_counts: tuple[EventCount, ...]
    conversion_rates: tuple[ConversionRate, ...]
    attribution_rows: tuple[AttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    abandoned_checkouts: int
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "engagement_counts": METRIC_ENGAGEMENT_COUNTS,
            "server_conversions": METRIC_SERVER_CONVERSIONS,
            "attribution": METRIC_ATTRIBUTION,
            "content_engagement": METRIC_CONTENT_ENGAGEMENT,
            "abandoned_checkouts": METRIC_ABANDONED_CHECKOUTS,
        }
    )


def parse_period_days(raw: str | None) -> int:
    """Return a bounded dashboard window in days (7, 30, or 90)."""
    if not raw:
        return DEFAULT_PERIOD_DAYS
    try:
        days = int(raw.strip())
    except ValueError:
        return DEFAULT_PERIOD_DAYS
    if days not in VALID_PERIOD_DAYS:
        return DEFAULT_PERIOD_DAYS
    return days


def _event_source(event_name: str) -> EventSource:
    return "server" if event_name in SERVER_ONLY_EVENTS else "browser"


def _rate_pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _count_lookup(counts: dict[str, int], event_name: str) -> int:
    return int(counts.get(event_name, 0))


def _build_event_counts(
    raw: list[tuple[str, int]],
    *,
    event_order: tuple[str, ...],
) -> tuple[EventCount, ...]:
    lookup = {name: count for name, count in raw}
    return tuple(
        EventCount(
            event_name=name,
            label=_EVENT_LABELS.get(name, name),
            count=_count_lookup(lookup, name),
            source=_event_source(name),
        )
        for name in event_order
    )


def build_conversion_rates(
    engagement: dict[str, int],
    server: dict[str, int],
) -> tuple[ConversionRate, ...]:
    """Build funnel conversion rates from event count lookups."""
    return _build_conversion_rates(engagement, server)


def _build_conversion_rates(
    engagement: dict[str, int],
    server: dict[str, int],
) -> tuple[ConversionRate, ...]:
    landing = _count_lookup(engagement, EVENT_LANDING_VIEWED)
    brief_viewed = _count_lookup(engagement, EVENT_BRIEF_VIEWED)
    form_started = _count_lookup(engagement, EVENT_BRIEF_FORM_STARTED)
    leads = _count_lookup(server, EVENT_LEAD_PERSISTED)
    checkouts = _count_lookup(server, EVENT_CHECKOUT_OPENED)
    payments = _count_lookup(server, EVENT_PAYMENT_COMPLETED)

    specs: list[tuple[str, str, int, int, str, str]] = [
        (
            "landing_to_brief",
            "Landing → brief page",
            brief_viewed,
            landing,
            "Brief Viewed (browser)",
            "Landing Viewed (browser)",
        ),
        (
            "brief_to_form",
            "Brief page → form start",
            form_started,
            brief_viewed,
            "Brief Form Started (browser)",
            "Brief Viewed (browser)",
        ),
        (
            "form_to_lead",
            "Form start → lead",
            leads,
            form_started,
            "Lead Persisted (server)",
            "Brief Form Started (browser)",
        ),
        (
            "lead_to_checkout",
            "Lead → checkout",
            checkouts,
            leads,
            "Checkout Opened (server)",
            "Lead Persisted (server)",
        ),
        (
            "checkout_to_payment",
            "Checkout → payment",
            payments,
            checkouts,
            "Payment Completed (server)",
            "Checkout Opened (server)",
        ),
        (
            "lead_to_payment",
            "Lead → payment",
            payments,
            leads,
            "Payment Completed (server)",
            "Lead Persisted (server)",
        ),
    ]
    return tuple(
        ConversionRate(
            key=key,
            label=label,
            numerator=numerator,
            denominator=denominator,
            rate_pct=_rate_pct(numerator, denominator),
            numerator_definition=num_def,
            denominator_definition=den_def,
        )
        for key, label, numerator, denominator, num_def, den_def in specs
    )


def _merge_attribution(
    event_rows: list[dict[str, Any]],
    brief_rows: list[dict[str, Any]],
) -> tuple[AttributionRow, ...]:
    merged: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in event_rows:
        key = (
            str(row.get("utm_source") or "(direct)"),
            str(row.get("utm_medium") or "(none)"),
            str(row.get("utm_campaign") or "(none)"),
        )
        bucket = merged.setdefault(key, {"engagement_events": 0, "leads": 0, "payments": 0})
        bucket["engagement_events"] += int(row.get("events") or 0)
    for row in brief_rows:
        key = (
            str(row.get("utm_source") or "(direct)"),
            str(row.get("utm_medium") or "(none)"),
            str(row.get("utm_campaign") or "(none)"),
        )
        bucket = merged.setdefault(key, {"engagement_events": 0, "leads": 0, "payments": 0})
        bucket["leads"] += int(row.get("leads") or 0)
        bucket["payments"] += int(row.get("payments") or 0)

    rows = [
        AttributionRow(
            utm_source=key[0],
            utm_medium=key[1],
            utm_campaign=key[2],
            engagement_events=values["engagement_events"],
            leads=values["leads"],
            payments=values["payments"],
        )
        for key, values in merged.items()
    ]
    rows.sort(key=lambda row: (row.leads, row.engagement_events), reverse=True)
    return tuple(rows[:DEFAULT_LIST_LIMIT])


def load_marketing_analytics(
    conn: psycopg.Connection,
    repo: MarketingAnalyticsRepository,
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    now: datetime | None = None,
    list_limit: int = DEFAULT_LIST_LIMIT,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
) -> MarketingAnalyticsData:
    """Load marketing analytics for a bounded UTC window."""
    if period_days not in VALID_PERIOD_DAYS:
        period_days = DEFAULT_PERIOD_DAYS
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    period_end = reference
    period_start = period_end - timedelta(days=period_days)

    engagement_raw = repo.count_engagement_events(
        conn,
        period_start=period_start,
        period_end=period_end,
        event_names=DASHBOARD_ENGAGEMENT_EVENTS,
    )
    server_raw = repo.count_server_conversion_events(
        conn,
        period_start=period_start,
        period_end=period_end,
        event_names=DASHBOARD_SERVER_EVENTS,
    )
    engagement_counts = _build_event_counts(
        engagement_raw,
        event_order=DASHBOARD_ENGAGEMENT_EVENTS,
    )
    server_counts = _build_event_counts(
        server_raw,
        event_order=DASHBOARD_SERVER_EVENTS,
    )
    engagement_lookup = {row.event_name: row.count for row in engagement_counts}
    server_lookup = {row.event_name: row.count for row in server_counts}

    attribution_rows = _merge_attribution(
        repo.count_attribution_from_events(
            conn,
            period_start=period_start,
            period_end=period_end,
            event_names=DASHBOARD_ENGAGEMENT_EVENTS,
            limit=list_limit,
        ),
        repo.count_attribution_from_briefs(
            conn,
            period_start=period_start,
            period_end=period_end,
            limit=list_limit,
        ),
    )

    case_study_engagement = tuple(
        ContentEngagementRow(
            content_type="case_study",
            slug=str(row["slug"]),
            views=int(row["views"]),
        )
        for row in repo.count_content_engagement(
            conn,
            period_start=period_start,
            period_end=period_end,
            event_name=EVENT_CASE_STUDY_VIEWED,
            slug_property="case_study_slug",
            limit=content_limit,
        )
    )
    article_engagement = tuple(
        ContentEngagementRow(
            content_type="article",
            slug=str(row["slug"]),
            views=int(row["views"]),
        )
        for row in repo.count_content_engagement(
            conn,
            period_start=period_start,
            period_end=period_end,
            event_name=EVENT_INSIGHT_VIEWED,
            slug_property="article_slug",
            limit=content_limit,
        )
    )

    abandoned = repo.count_abandoned_checkouts(
        conn,
        period_start=period_start,
        period_end=period_end,
    )

    return MarketingAnalyticsData(
        period_days=period_days,
        period_start=period_start,
        period_end=period_end,
        engagement_counts=engagement_counts,
        server_conversion_counts=server_counts,
        conversion_rates=build_conversion_rates(engagement_lookup, server_lookup),
        attribution_rows=attribution_rows,
        case_study_engagement=case_study_engagement,
        article_engagement=article_engagement,
        abandoned_checkouts=abandoned,
        generated_at=reference,
    )


def marketing_analytics_is_empty(data: MarketingAnalyticsData) -> bool:
    """True when every count and table is zero."""
    if data.abandoned_checkouts:
        return False
    if any(row.count for row in data.engagement_counts):
        return False
    if any(row.count for row in data.server_conversion_counts):
        return False
    if data.attribution_rows or data.case_study_engagement or data.article_engagement:
        return False
    return True


def render_marketing_analytics_csv(data: MarketingAnalyticsData) -> str:
    """Render aggregated marketing analytics as CSV (no row-level PII)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["section", "metric", "value", "source", "period_days"])
    writer.writerow(
        [
            "meta",
            "period_start",
            neutralize_csv_cell(data.period_start.isoformat()),
            "",
            data.period_days,
        ]
    )
    writer.writerow(
        [
            "meta",
            "period_end",
            neutralize_csv_cell(data.period_end.isoformat()),
            "",
            data.period_days,
        ]
    )

    for row in data.engagement_counts:
        writer.writerow(
            [
                "engagement",
                neutralize_csv_cell(row.label),
                row.count,
                row.source,
                data.period_days,
            ]
        )
    for row in data.server_conversion_counts:
        writer.writerow(
            [
                "server_conversion",
                neutralize_csv_cell(row.label),
                row.count,
                row.source,
                data.period_days,
            ]
        )
    for rate in data.conversion_rates:
        writer.writerow(
            [
                "conversion_rate",
                neutralize_csv_cell(rate.label),
                "" if rate.rate_pct is None else rate.rate_pct,
                f"{rate.numerator}/{rate.denominator}",
                data.period_days,
            ]
        )
    for row in data.attribution_rows:
        writer.writerow(
            [
                "attribution",
                neutralize_csv_cell(
                    f"{row.utm_source}|{row.utm_medium}|{row.utm_campaign}"
                ),
                f"engagement={row.engagement_events};leads={row.leads};payments={row.payments}",
                "aggregated",
                data.period_days,
            ]
        )
    for row in data.case_study_engagement:
        writer.writerow(
            [
                "content",
                neutralize_csv_cell(f"case_study:{row.slug}"),
                row.views,
                "browser",
                data.period_days,
            ]
        )
    for row in data.article_engagement:
        writer.writerow(
            [
                "content",
                neutralize_csv_cell(f"article:{row.slug}"),
                row.views,
                "browser",
                data.period_days,
            ]
        )
    writer.writerow(
        [
            "checkout",
            "abandoned_checkouts",
            data.abandoned_checkouts,
            "server",
            data.period_days,
        ]
    )
    return buffer.getvalue()
