"""Unit tests for marketing analytics dashboard queries and metrics (#116)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_dashboard import (
    AnalyticsDashboardData,
    build_conversion_rates,
    compute_rate_pct,
    dashboard_has_data,
    load_analytics_dashboard,
    parse_date_range,
)
from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.repositories.postgres import PostgresAnalyticsDashboardRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list[dict[str, object]]) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = None
    cursor.fetchall.return_value = rows
    conn.cursor.return_value = cursor
    return conn


@pytest.mark.unit
def test_parse_date_range_defaults_to_seven_days() -> None:
    parsed = parse_date_range(now=NOW)
    assert parsed.from_date == date(2026, 7, 9)
    assert parsed.to_date == date(2026, 7, 15)
    assert parsed.start == datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    assert parsed.end == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_date_range_swaps_inverted_bounds() -> None:
    parsed = parse_date_range(date_from="2026-07-20", date_to="2026-07-10", now=NOW)
    assert parsed.from_date == date(2026, 7, 10)
    assert parsed.to_date == date(2026, 7, 20)


@pytest.mark.unit
def test_parse_date_range_caps_at_ninety_days() -> None:
    parsed = parse_date_range(date_from="2026-01-01", date_to="2026-07-15", now=NOW)
    assert (parsed.to_date - parsed.from_date).days + 1 == 90


@pytest.mark.unit
def test_compute_rate_pct_handles_zero_denominator() -> None:
    assert compute_rate_pct(5, 0) is None
    assert compute_rate_pct(3, 10) == 30.0


@pytest.mark.unit
def test_build_conversion_rates_includes_explicit_labels() -> None:
    counts = {
        EVENT_LANDING_VIEWED: 100,
        EVENT_BRIEF_VIEWED: 40,
        EVENT_BRIEF_FORM_STARTED: 20,
        EVENT_LEAD_PERSISTED: 10,
        EVENT_CHECKOUT_OPENED: 8,
        EVENT_PAYMENT_COMPLETED: 4,
    }
    rates = build_conversion_rates(counts)
    brief_to_form = next(row for row in rates if row.key == "brief_to_form")
    assert brief_to_form.numerator == 20
    assert brief_to_form.denominator == 40
    assert brief_to_form.numerator_label == "Brief form started"
    assert brief_to_form.denominator_label == "Brief viewed"
    assert brief_to_form.rate_pct == 50.0


@pytest.mark.unit
def test_count_events_sql_is_bounded_and_indexed() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 12}])
    result = repo.count_events_by_name(
        conn,
        start=NOW - timedelta(days=7),
        end=NOW,
        event_names=(EVENT_LANDING_VIEWED, EVENT_BRIEF_VIEWED),
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "occurred_at >= %s" in sql
    assert "occurred_at < %s" in sql
    assert "event_name = ANY(%s)" in sql
    assert result[EVENT_LANDING_VIEWED] == 12


@pytest.mark.unit
def test_attribution_sql_groups_allowlisted_fields() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    conn = _mock_conn([])
    repo.list_attribution_breakdown(
        conn,
        start=NOW - timedelta(days=7),
        end=NOW,
        limit=20,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "utm_source" in sql
    assert "utm_medium" in sql
    assert "utm_campaign" in sql
    assert "Lead Persisted" in sql
    assert "LIMIT %s" in sql


@pytest.mark.unit
def test_content_engagement_sql_filters_slug() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    conn = _mock_conn([{"slug": "payments-platform", "views": 9}])
    rows = repo.list_content_engagement(
        conn,
        start=NOW - timedelta(days=7),
        end=NOW,
        event_name="Case Study Viewed",
        slug_property="case_study_slug",
        limit=15,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "properties->>" in sql
    assert "GROUP BY 1" in sql
    assert rows[0]["slug"] == "payments-platform"


@pytest.mark.unit
def test_content_engagement_rejects_unknown_slug_property() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.list_content_engagement(
            _mock_conn([]),
            start=NOW,
            end=NOW,
            event_name="Case Study Viewed",
            slug_property="visitor_id",
            limit=10,
        )


@pytest.mark.unit
def test_load_analytics_dashboard_maps_repository_rows() -> None:
    repo = MagicMock()
    repo.count_events_by_name.return_value = {
        EVENT_LANDING_VIEWED: 50,
        EVENT_BRIEF_VIEWED: 10,
        EVENT_BRIEF_FORM_STARTED: 5,
        EVENT_LEAD_PERSISTED: 2,
        EVENT_CHECKOUT_OPENED: 1,
        EVENT_PAYMENT_COMPLETED: 1,
    }
    repo.list_attribution_breakdown.return_value = [
        {
            "source": "linkedin",
            "medium": "social",
            "campaign": "spring-launch",
            "total_events": 20,
            "leads": 3,
        }
    ]
    repo.list_content_engagement.side_effect = [
        [{"slug": "edge-migration", "views": 7}],
        [{"slug": "diagnostic-readiness", "views": 4}],
    ]

    data = load_analytics_dashboard(MagicMock(), repo, now=NOW)
    assert data.engagement_counts[0].count == 50
    assert data.server_counts[0].count == 2
    assert data.attribution[0].source == "linkedin"
    assert data.case_studies[0].slug == "edge-migration"
    assert data.articles[0].slug == "diagnostic-readiness"
    assert data.generated_at == NOW


@pytest.mark.unit
def test_dashboard_has_data_false_for_empty_payload() -> None:
    empty = AnalyticsDashboardData(
        engagement_counts=(),
        server_counts=(),
        conversion_rates=(),
        attribution=(),
        case_studies=(),
        articles=(),
        generated_at=NOW,
        date_range=parse_date_range(now=NOW),
    )
    assert dashboard_has_data(empty) is False
