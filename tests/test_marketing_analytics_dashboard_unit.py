"""Unit tests for marketing analytics dashboard metrics and repository queries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.marketing_analytics_dashboard import (
    ATTRIBUTION_ROW_LIMIT,
    DASHBOARD_TIMEZONE,
    MAX_RANGE_DAYS,
    compute_rate_pct,
    load_marketing_analytics_dashboard,
    normalize_filters,
)
from app.repositories.marketing_analytics import PostgresMarketingAnalyticsRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


class _FakeMarketingAnalyticsRepository:
    def __init__(self) -> None:
        self.event_counts = [
            (EVENT_LANDING_VIEWED, 100),
            (EVENT_BRIEF_VIEWED, 40),
            (EVENT_BRIEF_FORM_STARTED, 10),
        ]
        self.server_counts = [
            (EVENT_LEAD_PERSISTED, 5),
            (EVENT_CHECKOUT_OPENED, 4),
            (EVENT_PAYMENT_COMPLETED, 3),
        ]
        self.engagement_attribution = [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "launch",
                "engagement_events": 20,
            }
        ]
        self.brief_attribution = [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "launch",
                "leads": 2,
                "payments": 1,
            }
        ]
        self.case_study_views = [("northwind-labs", 12)]
        self.article_views = [("first-party-analytics", 8)]

    def count_events_by_name(self, conn, *, start, end_exclusive, event_names):
        source = (
            self.server_counts
            if EVENT_LEAD_PERSISTED in event_names
            else self.event_counts
        )
        allowed = set(event_names)
        return [(name, count) for name, count in source if name in allowed]

    def count_content_views(
        self, conn, *, start, end_exclusive, event_name, slug_property, limit
    ):
        if slug_property == "case_study_slug":
            return self.case_study_views[:limit]
        return self.article_views[:limit]

    def count_engagement_attribution(self, conn, *, start, end_exclusive, limit):
        return self.engagement_attribution[:limit]

    def count_brief_attribution(self, conn, *, start, end_exclusive, limit):
        return self.brief_attribution[:limit]


@pytest.mark.unit
def test_normalize_filters_defaults_to_seven_day_window() -> None:
    filters = normalize_filters(reference=NOW)
    assert filters.end_date == date(2026, 7, 15)
    assert filters.start_date == date(2026, 7, 9)
    assert filters.end_exclusive == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_normalize_filters_swaps_inverted_dates() -> None:
    filters = normalize_filters(
        date_from="2026-07-20",
        date_to="2026-07-10",
        reference=NOW,
    )
    assert filters.start_date == date(2026, 7, 10)
    assert filters.end_date == date(2026, 7, 20)


@pytest.mark.unit
def test_normalize_filters_caps_range_at_max_days() -> None:
    filters = normalize_filters(
        date_from="2026-01-01",
        date_to="2026-07-15",
        reference=NOW,
    )
    span = (filters.end_date - filters.start_date).days + 1
    assert span == MAX_RANGE_DAYS


@pytest.mark.unit
def test_compute_rate_pct_handles_zero_denominator() -> None:
    assert compute_rate_pct(3, 0) is None
    assert compute_rate_pct(1, 4) == 25.0


@pytest.mark.unit
def test_load_marketing_analytics_dashboard_builds_conversion_rates() -> None:
    data = load_marketing_analytics_dashboard(
        MagicMock(),
        _FakeMarketingAnalyticsRepository(),
        date_from="2026-07-09",
        date_to="2026-07-15",
        reference=NOW,
    )
    assert data.filters.start_raw == "2026-07-09"
    assert data.filters.end_raw == "2026-07-15"
    assert DASHBOARD_TIMEZONE in data.metric_definitions["event_window"]
    assert any(row.event_name == EVENT_LANDING_VIEWED for row in data.engagement_events)
    assert any(row.event_name == EVENT_PAYMENT_COMPLETED for row in data.server_events)
    brief_rate = next(row for row in data.conversion_rates if "Brief view" in row.label)
    assert brief_rate.numerator == 10
    assert brief_rate.denominator == 40
    assert brief_rate.rate_pct == 25.0
    assert data.attribution[0].utm_source == "linkedin"
    assert data.attribution[0].engagement_events == 20
    assert data.attribution[0].leads == 2
    assert data.case_study_views[0].slug == "northwind-labs"


@pytest.mark.unit
def test_postgres_repository_event_count_query_is_bounded() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    start = NOW - timedelta(days=7)
    end_exclusive = NOW + timedelta(days=1)
    conn = _mock_conn(
        [
            {"event_name": EVENT_LANDING_VIEWED, "total": 12},
        ]
    )
    rows = repo.count_events_by_name(
        conn,
        start=start,
        end_exclusive=end_exclusive,
        event_names=(EVENT_LANDING_VIEWED,),
    )
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "occurred_at >=" in sql
    assert "occurred_at <" in sql
    assert rows == [(EVENT_LANDING_VIEWED, 12)]


@pytest.mark.unit
def test_postgres_repository_content_views_requires_allowlisted_slug() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.count_content_views(
            MagicMock(),
            start=NOW,
            end_exclusive=NOW + timedelta(days=1),
            event_name="Case Study Viewed",
            slug_property="visitor_id",
            limit=10,
        )


@pytest.mark.unit
def test_postgres_repository_attribution_rollup_limits_rows() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([{"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "x", "engagement_events": 3}])
    rows = repo.count_engagement_attribution(
        conn,
        start=NOW,
        end_exclusive=NOW + timedelta(days=1),
        limit=ATTRIBUTION_ROW_LIMIT,
    )
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "attribution->>'utm_source'" in sql
    assert "LIMIT" in sql
    assert rows[0]["utm_source"] == "linkedin"
