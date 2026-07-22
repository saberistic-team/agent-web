"""Unit tests for first-party marketing analytics dashboard (#116)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_dashboard import (
    ATTRIBUTION_LIMIT,
    CONTENT_ENGAGEMENT_LIMIT,
    MAX_DATE_RANGE_DAYS,
    METRIC_ATTRIBUTION,
    METRIC_CONVERSION_RATE,
    METRIC_EVENT_VOLUME,
    METRIC_SERVER_VS_BROWSER,
    AnalyticsDashboardData,
    build_conversion_rates,
    build_event_volumes,
    compute_conversion_rate,
    dashboard_has_activity,
    load_analytics_dashboard,
    parse_analytics_date_range,
)
from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.analytics_export import render_analytics_export_csv

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(fetchall_sequences: list[list[dict]] | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if fetchall_sequences is None:
        fetchall_sequences = [[], [], [], []]
    cur.fetchall.side_effect = fetchall_sequences
    return conn


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    data = AnalyticsDashboardData(
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 14),
        event_volumes=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert "occurred_at" in data.metric_definitions["event_volume"]
    assert "idempotency_key" in data.metric_definitions["event_volume"]
    assert "denominator" in METRIC_CONVERSION_RATE.lower()
    assert "utm_source" in METRIC_ATTRIBUTION
    assert "server-authoritative" in METRIC_SERVER_VS_BROWSER.lower()
    assert METRIC_EVENT_VOLUME == data.metric_definitions["event_volume"]


@pytest.mark.unit
def test_parse_analytics_date_range_defaults_to_seven_days() -> None:
    start, end, start_day, end_day = parse_analytics_date_range(None, None, now=NOW)
    assert start_day == date(2026, 7, 9)
    assert end_day == date(2026, 7, 15)
    assert end - start == end.replace(hour=0) - start.replace(hour=0)
    assert start.tzinfo == timezone.utc


@pytest.mark.unit
def test_parse_analytics_date_range_swaps_inverted_bounds() -> None:
    _, _, start_day, end_day = parse_analytics_date_range("2026-07-20", "2026-07-10", now=NOW)
    assert start_day == date(2026, 7, 10)
    assert end_day == date(2026, 7, 20)


@pytest.mark.unit
def test_parse_analytics_date_range_caps_at_max_days() -> None:
    _, _, start_day, end_day = parse_analytics_date_range("2026-01-01", "2026-07-15", now=NOW)
    assert (end_day - start_day).days + 1 == MAX_DATE_RANGE_DAYS


@pytest.mark.unit
def test_compute_conversion_rate_handles_zero_denominator() -> None:
    assert compute_conversion_rate(5, 0) is None
    assert compute_conversion_rate(0, 0) is None
    assert compute_conversion_rate(1, 4) == 25.0


@pytest.mark.unit
def test_build_conversion_rates_uses_explicit_definitions() -> None:
    counts = {
        EVENT_LANDING_VIEWED: 100,
        EVENT_BRIEF_FORM_STARTED: 20,
        EVENT_LEAD_PERSISTED: 10,
        EVENT_CHECKOUT_OPENED: 8,
        EVENT_PAYMENT_COMPLETED: 4,
    }
    rates = build_conversion_rates(counts)
    by_key = {row.key: row for row in rates}
    assert by_key["landing_to_brief_start"].rate_pct == 20.0
    assert by_key["checkout_to_paid"].numerator == 4
    assert by_key["checkout_to_paid"].denominator == 8
    assert "Lead Persisted (server)" in by_key["lead_to_checkout"].definition


@pytest.mark.unit
def test_build_event_volumes_marks_server_events() -> None:
    counts = {EVENT_LANDING_VIEWED: 3, EVENT_PAYMENT_COMPLETED: 1}
    rows = build_event_volumes(counts)
    landing = next(row for row in rows if row.event_name == EVENT_LANDING_VIEWED)
    paid = next(row for row in rows if row.event_name == EVENT_PAYMENT_COMPLETED)
    assert landing.source == "browser"
    assert paid.source == "server"


@pytest.mark.unit
def test_load_analytics_dashboard_queries_are_bounded() -> None:
    event_rows = [
        {"event_name": EVENT_LANDING_VIEWED, "total": 12},
        {"event_name": EVENT_BRIEF_FORM_STARTED, "total": 3},
    ]
    attribution_rows = [
        {
            "utm_source": "linkedin",
            "utm_medium": "social",
            "utm_campaign": "q3",
            "landing_views": 8,
            "brief_starts": 2,
            "leads": 1,
            "checkouts": 1,
            "payments": 0,
        }
    ]
    case_rows = [{"slug": "atlas-freight", "views": 5}]
    article_rows = [{"slug": "pipeline-velocity", "views": 4}]
    conn = _mock_conn([event_rows, attribution_rows, case_rows, article_rows])

    data = load_analytics_dashboard(conn, date_from="2026-07-10", date_to="2026-07-15", now=NOW)

    assert data.date_from == date(2026, 7, 10)
    assert data.date_to == date(2026, 7, 15)
    assert dashboard_has_activity(data) is True
    assert data.attribution_rows[0].utm_source == "linkedin"
    assert data.case_study_engagement[0].slug == "atlas-freight"

    calls = conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    event_sql = str(calls[0].args[0])
    attribution_sql = str(calls[1].args[0])
    case_sql = str(calls[2].args[0])
    attribution_limit = calls[1].args[1][-1]
    case_limit = calls[2].args[1][-1]
    assert "occurred_at >=" in event_sql
    assert "event_name = ANY" in event_sql
    assert "LIMIT %s" in attribution_sql
    assert "LIMIT %s" in case_sql
    assert attribution_limit == ATTRIBUTION_LIMIT
    assert case_limit == CONTENT_ENGAGEMENT_LIMIT


@pytest.mark.unit
def test_render_analytics_export_csv_is_aggregated_only() -> None:
    counts = {
        EVENT_LANDING_VIEWED: 10,
        EVENT_BRIEF_FORM_STARTED: 2,
        EVENT_LEAD_PERSISTED: 1,
        EVENT_CHECKOUT_OPENED: 1,
        EVENT_PAYMENT_COMPLETED: 0,
    }
    data = AnalyticsDashboardData(
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 14),
        event_volumes=build_event_volumes(counts),
        conversion_rates=build_conversion_rates(counts),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    csv_text = render_analytics_export_csv(data)
    assert "section,metric,value,source,definition" in csv_text
    assert "event_volume" in csv_text
    assert "conversion_rate" in csv_text
    assert "anonymous_session_id" not in csv_text
    assert "date_range" in csv_text
