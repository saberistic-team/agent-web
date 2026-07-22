"""First-party marketing analytics dashboard — funnel, attribution, and content engagement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

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

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_DATE_RANGE_DAYS = 7
MAX_DATE_RANGE_DAYS = 90
CONTENT_ENGAGEMENT_LIMIT = 20
ATTRIBUTION_LIMIT = 20

EventSource = Literal["browser", "server"]

METRIC_EVENT_VOLUME = (
    "Distinct stored events in the selected UTC window, filtered by occurred_at "
    "(not received_at). Duplicates are prevented at ingest via idempotency_key; "
    "bot/noise traffic rejected by ingest controls is excluded."
)
METRIC_CONVERSION_RATE = (
    "Numerator ÷ denominator × 100 for the same UTC occurred_at window. "
    "Returns no rate when the denominator is zero."
)
METRIC_ATTRIBUTION = (
    "Allowlisted utm_source, utm_medium, and utm_campaign from analytics_events.attribution "
    "JSONB only. Empty values roll up as (direct) / —. Aggregated counts — no session drill-down."
)
METRIC_CONTENT_ENGAGEMENT = (
    "Browser Case Study Viewed / Insight Viewed counts grouped by server-known slug "
    "from event properties. No per-visitor browsing history."
)
METRIC_SERVER_VS_BROWSER = (
    "Lead Persisted, Checkout Opened, and Payment Completed are server-authoritative "
    "conversion events. All other funnel rows are browser engagement signals."
)

SERVER_AUTHORITATIVE_EVENTS = frozenset(
    {
        EVENT_LEAD_PERSISTED,
        EVENT_CHECKOUT_OPENED,
        EVENT_PAYMENT_COMPLETED,
    }
)

DASHBOARD_EVENT_ORDER: tuple[tuple[str, EventSource, str], ...] = (
    (EVENT_LANDING_VIEWED, "browser", "Landing"),
    (EVENT_SERVICES_VIEWED, "browser", "Services"),
    (EVENT_CASE_STUDIES_VIEWED, "browser", "Case studies index"),
    (EVENT_CASE_STUDY_VIEWED, "browser", "Case study detail"),
    (EVENT_INSIGHTS_VIEWED, "browser", "Insights index"),
    (EVENT_INSIGHT_VIEWED, "browser", "Insight article"),
    (EVENT_BRIEF_VIEWED, "browser", "Brief / diagnostic"),
    (EVENT_BRIEF_FORM_STARTED, "browser", "Brief form started"),
    (EVENT_LEAD_PERSISTED, "server", "Lead persisted"),
    (EVENT_CHECKOUT_OPENED, "server", "Checkout opened"),
    (EVENT_PAYMENT_COMPLETED, "server", "Paid diagnostic"),
    (EVENT_CONTACT_INITIATED, "browser", "Contact initiated"),
)

CONVERSION_RATE_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "landing_to_brief_start",
        "Landing → brief start",
        EVENT_BRIEF_FORM_STARTED,
        EVENT_LANDING_VIEWED,
        "Brief Form Started (browser) ÷ Landing Viewed (browser)",
    ),
    (
        "brief_start_to_lead",
        "Brief start → lead",
        EVENT_LEAD_PERSISTED,
        EVENT_BRIEF_FORM_STARTED,
        "Lead Persisted (server) ÷ Brief Form Started (browser)",
    ),
    (
        "lead_to_checkout",
        "Lead → checkout",
        EVENT_CHECKOUT_OPENED,
        EVENT_LEAD_PERSISTED,
        "Checkout Opened (server) ÷ Lead Persisted (server)",
    ),
    (
        "checkout_to_paid",
        "Checkout → paid",
        EVENT_PAYMENT_COMPLETED,
        EVENT_CHECKOUT_OPENED,
        "Payment Completed (server) ÷ Checkout Opened (server)",
    ),
    (
        "landing_to_paid",
        "Landing → paid",
        EVENT_PAYMENT_COMPLETED,
        EVENT_LANDING_VIEWED,
        "Payment Completed (server) ÷ Landing Viewed (browser)",
    ),
)


@dataclass(frozen=True)
class EventVolumeRow:
    event_name: str
    label: str
    count: int
    source: EventSource


@dataclass(frozen=True)
class ConversionRateRow:
    key: str
    label: str
    numerator: int
    denominator: int
    rate_pct: float | None
    definition: str


@dataclass(frozen=True)
class AttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    landing_views: int
    brief_starts: int
    leads: int
    checkouts: int
    payments: int


@dataclass(frozen=True)
class ContentEngagementRow:
    content_type: Literal["case_study", "article"]
    slug: str
    views: int


@dataclass(frozen=True)
class AnalyticsDashboardData:
    date_from: date
    date_to: date
    event_volumes: tuple[EventVolumeRow, ...]
    conversion_rates: tuple[ConversionRateRow, ...]
    attribution_rows: tuple[AttributionRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "event_volume": METRIC_EVENT_VOLUME,
            "conversion_rate": METRIC_CONVERSION_RATE,
            "attribution": METRIC_ATTRIBUTION,
            "content_engagement": METRIC_CONTENT_ENGAGEMENT,
            "server_vs_browser": METRIC_SERVER_VS_BROWSER,
        }
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def parse_analytics_date_range(
    date_from: str | None,
    date_to: str | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime, date, date]:
    """Return UTC [start, end) bounds and inclusive calendar dates for the dashboard."""
    reference = now or _utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    end_day = _parse_date(date_to) if date_to else reference.date()
    start_day = (
        _parse_date(date_from)
        if date_from
        else end_day - timedelta(days=DEFAULT_DATE_RANGE_DAYS - 1)
    )

    if start_day > end_day:
        start_day, end_day = end_day, start_day

    span_days = (end_day - start_day).days + 1
    if span_days > MAX_DATE_RANGE_DAYS:
        start_day = end_day - timedelta(days=MAX_DATE_RANGE_DAYS - 1)

    range_start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    range_end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return range_start, range_end, start_day, end_day


def compute_conversion_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def build_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRateRow, ...]:
    rows: list[ConversionRateRow] = []
    for key, label, numerator_event, denominator_event, definition in CONVERSION_RATE_SPECS:
        numerator = int(counts.get(numerator_event, 0))
        denominator = int(counts.get(denominator_event, 0))
        rows.append(
            ConversionRateRow(
                key=key,
                label=label,
                numerator=numerator,
                denominator=denominator,
                rate_pct=compute_conversion_rate(numerator, denominator),
                definition=definition,
            )
        )
    return tuple(rows)


def build_event_volumes(counts: dict[str, int]) -> tuple[EventVolumeRow, ...]:
    return tuple(
        EventVolumeRow(
            event_name=event_name,
            label=label,
            count=int(counts.get(event_name, 0)),
            source=source,
        )
        for event_name, source, label in DASHBOARD_EVENT_ORDER
    )


def _count_events_by_name(
    conn: psycopg.Connection,
    *,
    range_start: datetime,
    range_end: datetime,
) -> dict[str, int]:
    event_names = tuple({spec[0] for spec in DASHBOARD_EVENT_ORDER})
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT event_name, COUNT(*)::int AS total
            FROM analytics_events
            WHERE occurred_at >= %s
              AND occurred_at < %s
              AND event_name = ANY(%s)
            GROUP BY event_name
            """,
            (range_start, range_end, list(event_names)),
        )
        rows = cur.fetchall()
    return {str(row["event_name"]): int(row["total"]) for row in rows}


def _load_attribution_rows(
    conn: psycopg.Connection,
    *,
    range_start: datetime,
    range_end: datetime,
    limit: int = ATTRIBUTION_LIMIT,
) -> tuple[AttributionRow, ...]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(attribution->>'utm_source'), ''), '(direct)') AS utm_source,
                COALESCE(NULLIF(TRIM(attribution->>'utm_medium'), ''), '—') AS utm_medium,
                COALESCE(NULLIF(TRIM(attribution->>'utm_campaign'), ''), '—') AS utm_campaign,
                COUNT(*) FILTER (WHERE event_name = %s)::int AS landing_views,
                COUNT(*) FILTER (WHERE event_name = %s)::int AS brief_starts,
                COUNT(*) FILTER (WHERE event_name = %s)::int AS leads,
                COUNT(*) FILTER (WHERE event_name = %s)::int AS checkouts,
                COUNT(*) FILTER (WHERE event_name = %s)::int AS payments
            FROM analytics_events
            WHERE occurred_at >= %s
              AND occurred_at < %s
            GROUP BY 1, 2, 3
            ORDER BY landing_views DESC, brief_starts DESC, leads DESC
            LIMIT %s
            """,
            (
                EVENT_LANDING_VIEWED,
                EVENT_BRIEF_FORM_STARTED,
                EVENT_LEAD_PERSISTED,
                EVENT_CHECKOUT_OPENED,
                EVENT_PAYMENT_COMPLETED,
                range_start,
                range_end,
                limit,
            ),
        )
        rows = cur.fetchall()
    return tuple(
        AttributionRow(
            utm_source=str(row["utm_source"]),
            utm_medium=str(row["utm_medium"]),
            utm_campaign=str(row["utm_campaign"]),
            landing_views=int(row["landing_views"]),
            brief_starts=int(row["brief_starts"]),
            leads=int(row["leads"]),
            checkouts=int(row["checkouts"]),
            payments=int(row["payments"]),
        )
        for row in rows
    )


def _load_content_engagement(
    conn: psycopg.Connection,
    *,
    range_start: datetime,
    range_end: datetime,
    event_name: str,
    slug_property: str,
    content_type: Literal["case_study", "article"],
    limit: int = CONTENT_ENGAGEMENT_LIMIT,
) -> tuple[ContentEngagementRow, ...]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT properties->>%s AS slug, COUNT(*)::int AS views
            FROM analytics_events
            WHERE occurred_at >= %s
              AND occurred_at < %s
              AND event_name = %s
              AND NULLIF(TRIM(properties->>%s), '') IS NOT NULL
            GROUP BY slug
            ORDER BY views DESC, slug ASC
            LIMIT %s
            """,
            (
                slug_property,
                range_start,
                range_end,
                event_name,
                slug_property,
                limit,
            ),
        )
        rows = cur.fetchall()
    return tuple(
        ContentEngagementRow(
            content_type=content_type,
            slug=str(row["slug"]),
            views=int(row["views"]),
        )
        for row in rows
    )


def load_analytics_dashboard(
    conn: psycopg.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDashboardData:
    range_start, range_end, start_day, end_day = parse_analytics_date_range(
        date_from,
        date_to,
        now=now,
    )
    counts = _count_events_by_name(conn, range_start=range_start, range_end=range_end)
    return AnalyticsDashboardData(
        date_from=start_day,
        date_to=end_day,
        event_volumes=build_event_volumes(counts),
        conversion_rates=build_conversion_rates(counts),
        attribution_rows=_load_attribution_rows(
            conn,
            range_start=range_start,
            range_end=range_end,
        ),
        case_study_engagement=_load_content_engagement(
            conn,
            range_start=range_start,
            range_end=range_end,
            event_name=EVENT_CASE_STUDY_VIEWED,
            slug_property="case_study_slug",
            content_type="case_study",
        ),
        article_engagement=_load_content_engagement(
            conn,
            range_start=range_start,
            range_end=range_end,
            event_name=EVENT_INSIGHT_VIEWED,
            slug_property="article_slug",
            content_type="article",
        ),
        generated_at=now or _utc_now(),
    )


def dashboard_has_activity(data: AnalyticsDashboardData) -> bool:
    return any(row.count > 0 for row in data.event_volumes)


def query_bounds_for_export(data: AnalyticsDashboardData) -> dict[str, str]:
    return {
        "from": data.date_from.isoformat(),
        "to": data.date_to.isoformat(),
    }
