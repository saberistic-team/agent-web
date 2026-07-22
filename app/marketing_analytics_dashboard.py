"""Marketing funnel, attribution, and content analytics for the admin dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import psycopg

from app.analytics_event_schema import (
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
    UTM_ATTRIBUTION_KEYS,
)
from app.brief_service import _parse_date_param
from app.repositories.protocols import MarketingAnalyticsRepository

DASHBOARD_TIMEZONE = "UTC"
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 90
ATTRIBUTION_LIMIT = 25
CONTENT_SLUG_LIMIT = 20

# Browser engagement — not authoritative for conversion truth.
ENGAGEMENT_EVENT_NAMES: tuple[str, ...] = (
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

# Client funnel signals that reuse server step numbers but are not authoritative.
SUPPLEMENTARY_CLIENT_EVENT_NAMES: tuple[str, ...] = (
    EVENT_CHECKOUT_CANCELLED,
    EVENT_BRIEF_SUCCESS_VIEWED,
)

# Authoritative server conversion events persisted after CRM/Stripe actions.
SERVER_CONVERSION_EVENT_NAMES: tuple[str, ...] = (
    EVENT_LEAD_PERSISTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_PAYMENT_COMPLETED,
)

ENGAGEMENT_EVENT_LABELS: dict[str, str] = {
    EVENT_LANDING_VIEWED: "Landing",
    EVENT_SERVICES_VIEWED: "Services",
    EVENT_CASE_STUDIES_VIEWED: "Case studies (index)",
    EVENT_CASE_STUDY_VIEWED: "Case study (detail)",
    EVENT_INSIGHTS_VIEWED: "Insights (index)",
    EVENT_INSIGHT_VIEWED: "Insight (detail)",
    EVENT_BRIEF_VIEWED: "Brief",
    EVENT_BRIEF_FORM_STARTED: "Brief form started",
    EVENT_CONTACT_INITIATED: "Contact initiated",
}

SERVER_EVENT_LABELS: dict[str, str] = {
    EVENT_LEAD_PERSISTED: "Lead persisted",
    EVENT_CHECKOUT_OPENED: "Checkout opened",
    EVENT_PAYMENT_COMPLETED: "Paid diagnostic",
}

SUPPLEMENTARY_EVENT_LABELS: dict[str, str] = {
    EVENT_CHECKOUT_CANCELLED: "Checkout cancelled (client)",
    EVENT_BRIEF_SUCCESS_VIEWED: "Brief success viewed (client)",
}

METRIC_ENGAGEMENT_EVENTS = (
    "Distinct analytics_events rows with consent_state != declined, "
    f"occurred_at in [{DASHBOARD_TIMEZONE}] range. Bots are rejected at ingest."
)
METRIC_SERVER_EVENTS = (
    "Authoritative server events persisted after brief create, Stripe checkout, "
    f"or payment webhook. Counted by occurred_at in [{DASHBOARD_TIMEZONE}] range."
)
METRIC_ATTRIBUTION = (
    "Aggregated allowlisted utm_source / utm_medium / utm_campaign only. "
    "Landing views from analytics_events; leads and payments from project_briefs."
)
METRIC_CONTENT = (
    "Aggregated page views by server-known article_slug or case_study_slug. "
    "No per-visitor session history is exposed."
)
METRIC_CONVERSION = (
    "Rates use explicit numerator/denominator counts from the same date window. "
    "Zero denominators render as em dash (—), not 0%."
)


@dataclass(frozen=True)
class AnalyticsDateRange:
    start: datetime
    end: datetime
    date_from: date | None
    date_to: date | None
    date_from_raw: str | None
    date_to_raw: str | None


@dataclass(frozen=True)
class EventCount:
    event_name: str
    label: str
    count: int
    source: str


@dataclass(frozen=True)
class ConversionRate:
    label: str
    numerator: int
    denominator: int
    rate_pct: float | None
    numerator_label: str
    denominator_label: str
    numerator_definition: str
    denominator_definition: str


@dataclass(frozen=True)
class AttributionRow:
    utm_source: str
    utm_medium: str
    utm_campaign: str
    landing_views: int
    leads: int
    payments: int


@dataclass(frozen=True)
class ContentEngagementRow:
    slug: str
    views: int
    content_type: str


@dataclass(frozen=True)
class MarketingAnalyticsDashboardData:
    engagement_counts: tuple[EventCount, ...]
    server_counts: tuple[EventCount, ...]
    supplementary_counts: tuple[EventCount, ...]
    conversion_rates: tuple[ConversionRate, ...]
    attribution_rows: tuple[AttributionRow, ...]
    article_engagement: tuple[ContentEngagementRow, ...]
    case_study_engagement: tuple[ContentEngagementRow, ...]
    date_range: AnalyticsDateRange
    generated_at: datetime
    metric_definitions: dict[str, str] = field(
        default_factory=lambda: {
            "engagement": METRIC_ENGAGEMENT_EVENTS,
            "server": METRIC_SERVER_EVENTS,
            "attribution": METRIC_ATTRIBUTION,
            "content": METRIC_CONTENT,
            "conversion": METRIC_CONVERSION,
        }
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _day_end_exclusive(value: date) -> datetime:
    return datetime.combine(value + timedelta(days=1), time.min, tzinfo=timezone.utc)


def parse_date_range(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> AnalyticsDateRange:
    """Parse bounded UTC date filters; default to the prior DEFAULT_RANGE_DAYS."""
    reference = now or _utc_now()
    today = reference.date()
    parsed_from = _parse_date_param(date_from)
    parsed_to = _parse_date_param(date_to)
    from_raw = date_from.strip()[:10] if date_from and date_from.strip() else None
    to_raw = date_to.strip()[:10] if date_to and date_to.strip() else None

    if parsed_from is None and parsed_to is None:
        parsed_to = today
        parsed_from = today - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        from_raw = parsed_from.isoformat()
        to_raw = parsed_to.isoformat()
    elif parsed_from is None:
        parsed_from = parsed_to
        from_raw = parsed_to.isoformat() if parsed_to else from_raw
    elif parsed_to is None:
        parsed_to = parsed_from
        to_raw = parsed_from.isoformat() if parsed_from else to_raw

    assert parsed_from is not None and parsed_to is not None
    if parsed_from > parsed_to:
        parsed_from, parsed_to = parsed_to, parsed_from
        from_raw, to_raw = to_raw, from_raw

    span_days = (parsed_to - parsed_from).days + 1
    if span_days > MAX_RANGE_DAYS:
        parsed_from = parsed_to - timedelta(days=MAX_RANGE_DAYS - 1)
        from_raw = parsed_from.isoformat()

    return AnalyticsDateRange(
        start=_day_start(parsed_from),
        end=_day_end_exclusive(parsed_to),
        date_from=parsed_from,
        date_to=parsed_to,
        date_from_raw=from_raw,
        date_to_raw=to_raw,
    )


def compute_rate_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _event_counts(
    raw: dict[str, int],
    event_names: tuple[str, ...],
    labels: dict[str, str],
    *,
    source: str,
) -> tuple[EventCount, ...]:
    return tuple(
        EventCount(
            event_name=name,
            label=labels.get(name, name),
            count=int(raw.get(name, 0)),
            source=source,
        )
        for name in event_names
    )


def _build_conversion_rates(counts: dict[str, int]) -> tuple[ConversionRate, ...]:
    landing = counts.get(EVENT_LANDING_VIEWED, 0)
    brief_start = counts.get(EVENT_BRIEF_FORM_STARTED, 0)
    lead = counts.get(EVENT_LEAD_PERSISTED, 0)
    checkout = counts.get(EVENT_CHECKOUT_OPENED, 0)
    payment = counts.get(EVENT_PAYMENT_COMPLETED, 0)

    specs: tuple[tuple[str, int, int, str, str, str, str], ...] = (
        (
            "Brief start rate",
            brief_start,
            landing,
            "Brief form started",
            "Landing views",
            "analytics_events Brief Form Started",
            "analytics_events Landing Viewed",
        ),
        (
            "Lead rate",
            lead,
            brief_start,
            "Lead persisted",
            "Brief form started",
            "server Lead Persisted",
            "analytics_events Brief Form Started",
        ),
        (
            "Checkout rate",
            checkout,
            lead,
            "Checkout opened",
            "Lead persisted",
            "server Checkout Opened",
            "server Lead Persisted",
        ),
        (
            "Payment rate",
            payment,
            checkout,
            "Paid diagnostic",
            "Checkout opened",
            "server Payment Completed",
            "server Checkout Opened",
        ),
        (
            "Landing to paid",
            payment,
            landing,
            "Paid diagnostic",
            "Landing views",
            "server Payment Completed",
            "analytics_events Landing Viewed",
        ),
    )
    return tuple(
        ConversionRate(
            label=label,
            numerator=numerator,
            denominator=denominator,
            rate_pct=compute_rate_pct(numerator, denominator),
            numerator_label=num_label,
            denominator_label=den_label,
            numerator_definition=num_def,
            denominator_definition=den_def,
        )
        for label, numerator, denominator, num_label, den_label, num_def, den_def in specs
    )


def _parse_attribution_rows(rows: list[dict[str, Any]]) -> tuple[AttributionRow, ...]:
    parsed: list[AttributionRow] = []
    for row in rows:
        parsed.append(
            AttributionRow(
                utm_source=str(row.get("utm_source") or "(direct)"),
                utm_medium=str(row.get("utm_medium") or "—"),
                utm_campaign=str(row.get("utm_campaign") or "—"),
                landing_views=int(row.get("landing_views") or 0),
                leads=int(row.get("leads") or 0),
                payments=int(row.get("payments") or 0),
            )
        )
    return tuple(parsed)


def _parse_content_rows(
    rows: list[dict[str, Any]],
    *,
    content_type: str,
) -> tuple[ContentEngagementRow, ...]:
    return tuple(
        ContentEngagementRow(
            slug=str(row["slug"]),
            views=int(row["views"]),
            content_type=content_type,
        )
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
    attribution_limit: int = ATTRIBUTION_LIMIT,
    content_limit: int = CONTENT_SLUG_LIMIT,
) -> MarketingAnalyticsDashboardData:
    """Load aggregated marketing analytics for the selected UTC date range."""
    date_range = parse_date_range(date_from=date_from, date_to=date_to, now=now)
    generated = now or _utc_now()

    engagement_raw = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=ENGAGEMENT_EVENT_NAMES,
        authoritative_only=False,
    )
    supplementary_raw = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=SUPPLEMENTARY_CLIENT_EVENT_NAMES,
        authoritative_only=False,
    )
    server_raw = repo.count_events_by_name(
        conn,
        start=date_range.start,
        end=date_range.end,
        event_names=SERVER_CONVERSION_EVENT_NAMES,
        authoritative_only=True,
    )

    merged_counts = {**engagement_raw, **server_raw}
    attribution_rows = _parse_attribution_rows(
        repo.list_attribution_summary(
            conn,
            start=date_range.start,
            end=date_range.end,
            limit=attribution_limit,
        )
    )
    article_rows = _parse_content_rows(
        repo.list_content_engagement(
            conn,
            start=date_range.start,
            end=date_range.end,
            event_name=EVENT_INSIGHT_VIEWED,
            slug_property="article_slug",
            limit=content_limit,
        ),
        content_type="article",
    )
    case_study_rows = _parse_content_rows(
        repo.list_content_engagement(
            conn,
            start=date_range.start,
            end=date_range.end,
            event_name=EVENT_CASE_STUDY_VIEWED,
            slug_property="case_study_slug",
            limit=content_limit,
        ),
        content_type="case_study",
    )

    return MarketingAnalyticsDashboardData(
        engagement_counts=_event_counts(
            engagement_raw,
            ENGAGEMENT_EVENT_NAMES,
            ENGAGEMENT_EVENT_LABELS,
            source="browser",
        ),
        server_counts=_event_counts(
            server_raw,
            SERVER_CONVERSION_EVENT_NAMES,
            SERVER_EVENT_LABELS,
            source="server",
        ),
        supplementary_counts=_event_counts(
            supplementary_raw,
            SUPPLEMENTARY_CLIENT_EVENT_NAMES,
            SUPPLEMENTARY_EVENT_LABELS,
            source="browser",
        ),
        conversion_rates=_build_conversion_rates(merged_counts),
        attribution_rows=attribution_rows,
        article_engagement=article_rows,
        case_study_engagement=case_study_rows,
        date_range=date_range,
        generated_at=generated,
    )


def dashboard_has_data(data: MarketingAnalyticsDashboardData) -> bool:
    """True when any section has non-zero aggregates."""
    if any(row.count for row in data.engagement_counts):
        return True
    if any(row.count for row in data.server_counts):
        return True
    if any(row.count for row in data.supplementary_counts):
        return True
    if data.attribution_rows:
        return True
    if data.article_engagement or data.case_study_engagement:
        return True
    return False


def empty_marketing_analytics_dashboard(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> MarketingAnalyticsDashboardData:
    """Zero-filled dashboard for DB errors or missing configuration."""
    date_range = parse_date_range(date_from=date_from, date_to=date_to, now=now)
    return MarketingAnalyticsDashboardData(
        engagement_counts=_event_counts({}, ENGAGEMENT_EVENT_NAMES, ENGAGEMENT_EVENT_LABELS, source="browser"),
        server_counts=_event_counts({}, SERVER_CONVERSION_EVENT_NAMES, SERVER_EVENT_LABELS, source="server"),
        supplementary_counts=_event_counts(
            {},
            SUPPLEMENTARY_CLIENT_EVENT_NAMES,
            SUPPLEMENTARY_EVENT_LABELS,
            source="browser",
        ),
        conversion_rates=_build_conversion_rates({}),
        attribution_rows=(),
        article_engagement=(),
        case_study_engagement=(),
        date_range=date_range,
        generated_at=now or _utc_now(),
    )


def serialize_dashboard_csv(data: MarketingAnalyticsDashboardData) -> str:
    """Render aggregated dashboard metrics as CSV (no row-level events)."""
    lines: list[str] = [
        "section,metric,numerator,denominator,rate_pct,count,utm_source,utm_medium,utm_campaign,slug",
    ]
    window = (
        f"{data.date_range.date_from_raw or ''}..{data.date_range.date_to_raw or ''}"
    )
    lines.append(f"meta,date_range,,,,{window},,,")

    for row in data.engagement_counts:
        lines.append(f"engagement,{_csv_cell(row.label)},,,,{row.count},,,,")
    for row in data.server_counts:
        lines.append(f"server,{_csv_cell(row.label)},,,,{row.count},,,,")
    for row in data.supplementary_counts:
        lines.append(f"supplementary,{_csv_cell(row.label)},,,,{row.count},,,,")

    for rate in data.conversion_rates:
        rate_value = "" if rate.rate_pct is None else str(rate.rate_pct)
        lines.append(
            f"conversion,{_csv_cell(rate.label)},{rate.numerator},{rate.denominator},"
            f"{rate_value},,,,,"
        )

    for row in data.attribution_rows:
        lines.append(
            f"attribution,combined,,,,{row.landing_views},"
            f"{_csv_cell(row.utm_source)},{_csv_cell(row.utm_medium)},{_csv_cell(row.utm_campaign)},"
        )
        if row.leads or row.payments:
            lines.append(
                f"attribution,leads_payments,{row.leads},{row.payments},,,"
                f"{_csv_cell(row.utm_source)},{_csv_cell(row.utm_medium)},{_csv_cell(row.utm_campaign)},"
            )

    for row in data.article_engagement:
        lines.append(f"content_article,{_csv_cell(row.slug)},,,,{row.views},,,,{_csv_cell(row.slug)}")
    for row in data.case_study_engagement:
        lines.append(
            f"content_case_study,{_csv_cell(row.slug)},,,,{row.views},,,,{_csv_cell(row.slug)}"
        )

    return "\n".join(lines) + "\n"


def _csv_cell(value: str) -> str:
    if any(char in value for char in (",", '"', "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value


def allowlisted_attribution_keys() -> frozenset[str]:
    return UTM_ATTRIBUTION_KEYS
