"""Unit tests for first-party marketing analytics dashboard (#116)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_dashboard import (
    ALLOWED_RANGE_PRESETS,
    AnalyticsDashboardData,
    AnalyticsDateRange,
    CrmFunnelCounts,
    MAX_ATTRIBUTION_ROWS,
    MAX_CONTENT_ROWS,
    MAX_RANGE_DAYS,
    METRIC_ATTRIBUTION,
    METRIC_EVENT_VOLUME,
    build_conversion_rates,
    dashboard_has_activity,
    format_conversion_rate,
    load_analytics_dashboard,
    parse_analytics_date_range,
)
from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.analytics_export import render_analytics_dashboard_csv
from app.repositories.postgres_analytics_dashboard import PostgresAnalyticsDashboardRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


class _FakeAnalyticsDashboardRepository:
    def count_events_by_name(self, conn, *, start, end, event_names):
        return {
            EVENT_LANDING_VIEWED: 100,
            EVENT_BRIEF_VIEWED: 40,
            EVENT_BRIEF_FORM_STARTED: 10,
            EVENT_LEAD_PERSISTED: 5,
            EVENT_CHECKOUT_OPENED: 4,
            EVENT_PAYMENT_COMPLETED: 3,
        }

    def count_crm_funnel(self, conn, *, start, end):
        return {"leads": 5, "checkouts": 4, "payments": 3}

    def list_attribution_buckets(self, conn, *, start, end, dimension, limit):
        return [
            {
                "key": "linkedin",
                "event_count": 20,
                "lead_count": 2,
            }
        ]

    def list_content_engagement(
        self, conn, *, start, end, event_name, slug_property, limit
    ):
        if slug_property == "case_study_slug":
            return [{"slug": "northwind-labs", "views": 12}]
        return [{"slug": "first-party-analytics", "views": 8}]


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    data = AnalyticsDashboardData(
        date_range=parse_analytics_date_range(now=NOW),
        event_counts=(),
        crm_counts=CrmFunnelCounts(leads=0, checkouts=0, payments=0),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert "occurred_at" in data.metric_definitions["event_volume"]
    assert "idempotency" in data.metric_definitions["event_volume"]
    assert "utm_source" in METRIC_ATTRIBUTION
    assert METRIC_EVENT_VOLUME == data.metric_definitions["event_volume"]


@pytest.mark.unit
def test_parse_analytics_date_range_defaults_to_seven_days() -> None:
    date_range = parse_analytics_date_range(now=NOW)
    assert date_range.preset_days == 7
    assert date_range.label == "Last 7 days"
    assert date_range.start.tzinfo == timezone.utc


@pytest.mark.unit
def test_parse_analytics_date_range_custom_range() -> None:
    date_range = parse_analytics_date_range(
        start="2026-07-09",
        end="2026-07-15",
        now=NOW,
    )
    assert date_range.preset_days is None
    assert date_range.start.date() == date(2026, 7, 9)
    assert (date_range.end - date_range.start).days == 7


@pytest.mark.unit
def test_parse_analytics_date_range_rejects_overlong_custom_range() -> None:
    with pytest.raises(ValueError, match=str(MAX_RANGE_DAYS)):
        parse_analytics_date_range(start="2026-01-01", end="2026-07-15", now=NOW)


@pytest.mark.unit
def test_parse_analytics_date_range_rejects_invalid_preset() -> None:
    with pytest.raises(ValueError, match=str(ALLOWED_RANGE_PRESETS)):
        parse_analytics_date_range(days="14", now=NOW)


@pytest.mark.unit
def test_format_conversion_rate_handles_zero_denominator() -> None:
    assert format_conversion_rate(3, 0) is None
    assert format_conversion_rate(1, 4) == 25.0


@pytest.mark.unit
def test_build_conversion_rates_uses_explicit_definitions() -> None:
    counts = {
        EVENT_LANDING_VIEWED: 100,
        EVENT_BRIEF_VIEWED: 40,
        EVENT_BRIEF_FORM_STARTED: 10,
        EVENT_LEAD_PERSISTED: 5,
        EVENT_CHECKOUT_OPENED: 4,
        EVENT_PAYMENT_COMPLETED: 3,
    }
    crm = CrmFunnelCounts(leads=5, checkouts=4, payments=3)
    rates = build_conversion_rates(event_counts=counts, crm_counts=crm)
    brief_rate = next(row for row in rates if "Lead persisted" in row.label)
    assert brief_rate.numerator == 5
    assert brief_rate.denominator == 10
    assert brief_rate.rate_percent == 50.0
    assert "Lead Persisted" in brief_rate.numerator_definition


@pytest.mark.unit
def test_load_analytics_dashboard_builds_sections() -> None:
    date_range = parse_analytics_date_range(start="2026-07-09", end="2026-07-15", now=NOW)
    data = load_analytics_dashboard(
        MagicMock(),
        _FakeAnalyticsDashboardRepository(),
        date_range=date_range,
        now=NOW,
    )
    assert dashboard_has_activity(data) is True
    assert any(row.event_name == EVENT_LANDING_VIEWED for row in data.event_counts)
    assert data.crm_counts.leads == 5
    assert data.attribution[0].key == "linkedin"
    assert data.case_study_engagement[0].slug == "northwind-labs"


@pytest.mark.unit
def test_postgres_repository_event_count_query_is_bounded() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(day=start.day + 1)
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 12}])
    counts = repo.count_events_by_name(
        conn,
        start=start,
        end=end,
        event_names=[EVENT_LANDING_VIEWED],
    )
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    assert "occurred_at >=" in sql
    assert "occurred_at <" in sql
    assert counts[EVENT_LANDING_VIEWED] == 12


@pytest.mark.unit
def test_postgres_repository_content_views_requires_allowlisted_slug() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.list_content_engagement(
            MagicMock(),
            start=NOW,
            end=NOW.replace(day=NOW.day + 1),
            event_name="Case Study Viewed",
            slug_property="visitor_id",
            limit=10,
        )


@pytest.mark.unit
def test_postgres_repository_attribution_query_limits_rows() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    conn = _mock_conn([{"key": "linkedin", "event_count": 3}])
    conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = [
        [{"key": "linkedin", "event_count": 3}],
        [{"key": "linkedin", "lead_count": 1}],
    ]
    rows = repo.list_attribution_buckets(
        conn,
        start=NOW,
        end=NOW.replace(day=NOW.day + 1),
        dimension="utm_source",
        limit=MAX_ATTRIBUTION_ROWS,
    )
    first_sql = conn.cursor.return_value.__enter__.return_value.execute.call_args_list[0].args[0]
    assert "LIMIT %s" in first_sql
    assert rows[0]["key"] == "linkedin"
    assert MAX_CONTENT_ROWS == 25


@pytest.mark.unit
def test_render_analytics_dashboard_csv_is_aggregated_only() -> None:
    date_range = parse_analytics_date_range(now=NOW)
    counts = {
        EVENT_LANDING_VIEWED: 10,
        EVENT_BRIEF_FORM_STARTED: 2,
        EVENT_LEAD_PERSISTED: 1,
        EVENT_CHECKOUT_OPENED: 1,
        EVENT_PAYMENT_COMPLETED: 0,
    }
    data = AnalyticsDashboardData(
        date_range=date_range,
        event_counts=(),
        crm_counts=CrmFunnelCounts(leads=1, checkouts=1, payments=0),
        conversion_rates=build_conversion_rates(
            event_counts=counts,
            crm_counts=CrmFunnelCounts(leads=1, checkouts=1, payments=0),
        ),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    csv_text = render_analytics_dashboard_csv(data)
    assert "section,metric,value,detail" in csv_text
    assert "event_volume" in csv_text or "conversion_rate" in csv_text
    assert "anonymous_session_id" not in csv_text
