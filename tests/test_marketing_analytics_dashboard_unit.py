"""Unit tests for marketing analytics dashboard metrics and queries."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.marketing_analytics_dashboard import (
    DASHBOARD_TIMEZONE,
    MAX_DATE_RANGE_DAYS,
    METRIC_CONVERSION_RATES,
    METRIC_ENGAGEMENT_EVENTS,
    METRIC_SERVER_CONVERSION_EVENTS,
    ConversionRateRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    compute_conversion_rate,
    dashboard_is_empty,
    load_marketing_analytics_dashboard,
    parse_analytics_date_range,
    render_analytics_csv,
)
from app.repositories.postgres import PostgresMarketingAnalyticsRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


class _FakeMarketingRepo:
    def count_events_by_name(self, conn, *, window_start, window_end, event_names):
        return [
            (EVENT_LANDING_VIEWED, 100),
            (EVENT_BRIEF_FORM_STARTED, 20),
            (EVENT_LEAD_PERSISTED, 8),
            (EVENT_CHECKOUT_OPENED, 6),
            (EVENT_PAYMENT_COMPLETED, 4),
        ]

    def list_event_attribution(self, conn, *, window_start, window_end, limit):
        return [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "spring-launch",
                "event_count": 42,
            }
        ]

    def list_lead_attribution(self, conn, *, window_start, window_end, limit):
        return [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "spring-launch",
                "leads": 5,
                "payments": 2,
            }
        ]

    def list_case_study_engagement(self, conn, *, window_start, window_end, limit):
        return [{"slug": "payments-platform", "views": 17}]

    def list_article_engagement(self, conn, *, window_start, window_end, limit):
        return [{"slug": "diagnostic-playbook", "views": 11}]


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    date_range = parse_analytics_date_range(reference=NOW)
    data = MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=(),
        server_conversion_events=(),
        client_supplementary_events=(),
        conversion_rates=(),
        event_attribution=(),
        lead_attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert "occurred_at" in data.metric_definitions["engagement_events"]
    assert "idempotency_key" in data.metric_definitions["engagement_events"]
    assert METRIC_ENGAGEMENT_EVENTS == data.metric_definitions["engagement_events"]
    assert "authoritative" in data.metric_definitions["server_conversion_events"].lower()
    assert METRIC_SERVER_CONVERSION_EVENTS == data.metric_definitions["server_conversion_events"]
    assert METRIC_CONVERSION_RATES == data.metric_definitions["conversion_rates"]


@pytest.mark.unit
def test_parse_analytics_date_range_defaults_to_seven_days() -> None:
    date_range = parse_analytics_date_range(reference=NOW)
    assert date_range.date_to == date(2026, 7, 15)
    assert date_range.date_from == date(2026, 7, 9)
    assert date_range.window_start.tzinfo == timezone.utc
    assert date_range.window_end == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_analytics_date_range_swaps_and_caps_span() -> None:
    date_range = parse_analytics_date_range(
        date_from="2026-01-01",
        date_to="2025-12-01",
        reference=NOW,
    )
    assert date_range.date_from <= date_range.date_to
    span = (date_range.date_to - date_range.date_from).days + 1
    assert span <= MAX_DATE_RANGE_DAYS


@pytest.mark.unit
def test_compute_conversion_rate_handles_zero_denominator() -> None:
    assert compute_conversion_rate(5, 0) is None
    assert compute_conversion_rate(0, 10) == 0.0
    assert compute_conversion_rate(1, 4) == 25.0


@pytest.mark.unit
def test_load_marketing_analytics_dashboard_maps_repository_rows() -> None:
    data = load_marketing_analytics_dashboard(
        MagicMock(),
        _FakeMarketingRepo(),
        now=NOW,
    )
    landing = next(row for row in data.engagement_events if row.event_name == EVENT_LANDING_VIEWED)
    assert landing.count == 100
    assert landing.source == "browser"
    lead = next(
        row for row in data.server_conversion_events if row.event_name == EVENT_LEAD_PERSISTED
    )
    assert lead.source == "server"
    form_to_lead = next(row for row in data.conversion_rates if row.key == "form_to_lead")
    assert form_to_lead.numerator == 8
    assert form_to_lead.denominator == 20
    assert form_to_lead.rate_pct == 40.0
    assert data.event_attribution[0].utm_source == "linkedin"
    assert data.lead_attribution[0].payments == 2
    assert data.case_study_engagement[0].slug == "payments-platform"


@pytest.mark.unit
def test_dashboard_is_empty() -> None:
    date_range = parse_analytics_date_range(reference=NOW)
    empty = MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=(EventCountRow("Landing Viewed", 0, "browser"),),
        server_conversion_events=(),
        client_supplementary_events=(),
        conversion_rates=(),
        event_attribution=(),
        lead_attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert dashboard_is_empty(empty) is True


@pytest.mark.unit
def test_render_analytics_csv_is_aggregated_only() -> None:
    date_range = parse_analytics_date_range(reference=NOW)
    data = MarketingAnalyticsDashboardData(
        date_range=date_range,
        engagement_events=(EventCountRow(EVENT_LANDING_VIEWED, 10, "browser"),),
        server_conversion_events=(EventCountRow(EVENT_LEAD_PERSISTED, 2, "server"),),
        client_supplementary_events=(),
        conversion_rates=(
            ConversionRateRow(
                key="form_to_lead",
                label="Brief form → lead persisted",
                numerator_event=EVENT_LEAD_PERSISTED,
                denominator_event=EVENT_BRIEF_FORM_STARTED,
                numerator=2,
                denominator=5,
                rate_pct=40.0,
                definition="test",
            ),
        ),
        event_attribution=(),
        lead_attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    csv_text = render_analytics_csv(data)
    assert "anonymous_session_id" not in csv_text
    assert "Landing Viewed" in csv_text
    assert DASHBOARD_TIMEZONE in csv_text
    assert "conversion_rate" in csv_text


@pytest.mark.unit
def test_postgres_marketing_repo_event_count_query_uses_occurred_at_window() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 3}])
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = repo.count_events_by_name(
        conn,
        window_start=start,
        window_end=end,
        event_names=[EVENT_LANDING_VIEWED],
    )
    assert rows == [(EVENT_LANDING_VIEWED, 3)]
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "occurred_at >=" in sql
    assert "occurred_at <" in sql
    assert "event_name = ANY" in sql


@pytest.mark.unit
def test_postgres_marketing_repo_attribution_groups_allowlisted_keys() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn(
        [
            {
                "utm_source": "(direct)",
                "utm_medium": "(none)",
                "utm_campaign": "(none)",
                "event_count": 9,
            }
        ]
    )
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, tzinfo=timezone.utc)
    rows = repo.list_event_attribution(
        conn,
        window_start=start,
        window_end=end,
        limit=20,
    )
    assert rows[0]["utm_source"] == "(direct)"
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "utm_source" in sql
    assert "utm_medium" in sql
    assert "utm_campaign" in sql
    assert "LIMIT" in sql
