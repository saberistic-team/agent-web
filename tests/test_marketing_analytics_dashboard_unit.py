"""Unit tests for marketing analytics dashboard logic."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_event_schema import (
    EVENT_CASE_STUDY_VIEWED,
    EVENT_INSIGHT_VIEWED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
)
from app.marketing_analytics_dashboard import (
    AnalyticsDateRange,
    compute_rate_pct,
    dashboard_has_data,
    empty_marketing_analytics_dashboard,
    load_marketing_analytics_dashboard,
    parse_date_range,
    serialize_dashboard_csv,
    _build_conversion_rates,
)
from app.repositories.postgres import PostgresMarketingAnalyticsRepository


UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
START = datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
END = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = rows or []
    return conn


@pytest.mark.unit
def test_parse_date_range_defaults_to_seven_days() -> None:
    parsed = parse_date_range(now=NOW)
    assert parsed.date_from == date(2026, 7, 9)
    assert parsed.date_to == date(2026, 7, 15)
    assert parsed.date_from_raw == "2026-07-09"
    assert parsed.date_to_raw == "2026-07-15"


@pytest.mark.unit
def test_parse_date_range_swaps_inverted_bounds() -> None:
    parsed = parse_date_range(date_from="2026-07-20", date_to="2026-07-10", now=NOW)
    assert parsed.date_from == date(2026, 7, 10)
    assert parsed.date_to == date(2026, 7, 20)


@pytest.mark.unit
def test_parse_date_range_caps_at_max_days() -> None:
    parsed = parse_date_range(date_from="2026-01-01", date_to="2026-07-15", now=NOW)
    assert parsed.date_to == date(2026, 7, 15)
    assert (parsed.date_to - parsed.date_from).days == 89


@pytest.mark.unit
def test_compute_rate_pct_handles_zero_denominator() -> None:
    assert compute_rate_pct(5, 0) is None
    assert compute_rate_pct(0, 0) is None
    assert compute_rate_pct(1, 4) == 25.0


@pytest.mark.unit
def test_build_conversion_rates_zero_denominators() -> None:
    rates = _build_conversion_rates({})
    assert len(rates) == 5
    assert all(rate.rate_pct is None for rate in rates)
    assert rates[0].numerator == 0
    assert rates[0].denominator == 0


@pytest.mark.unit
def test_empty_dashboard_has_zero_counts() -> None:
    data = empty_marketing_analytics_dashboard(now=NOW)
    assert all(row.count == 0 for row in data.engagement_counts)
    assert all(row.count == 0 for row in data.server_counts)
    assert data.attribution_rows == ()


@pytest.mark.unit
def test_serialize_dashboard_csv_is_aggregated_only() -> None:
    data = empty_marketing_analytics_dashboard(
        date_from="2026-07-01",
        date_to="2026-07-07",
        now=NOW,
    )
    csv_text = serialize_dashboard_csv(data)
    assert csv_text.startswith("section,metric,numerator,denominator,rate_pct,count")
    assert "engagement,Landing" in csv_text
    assert "server,Lead persisted" in csv_text
    assert "conversion,Brief start rate,0,0,," in csv_text
    assert "anonymous_session" not in csv_text.lower()


@pytest.mark.unit
def test_date_range_end_is_exclusive_next_day() -> None:
    parsed = parse_date_range(date_from="2026-07-01", date_to="2026-07-01", now=NOW)
    assert isinstance(parsed, AnalyticsDateRange)
    assert parsed.end > parsed.start


@pytest.mark.unit
def test_count_events_by_name_sql_is_bounded() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 42}])
    counts = repo.count_events_by_name(
        conn,
        start=START,
        end=END,
        event_names=(EVENT_LANDING_VIEWED,),
        authoritative_only=False,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert "occurred_at >= %s" in sql
    assert "occurred_at < %s" in sql
    assert "event_name = ANY(%s)" in sql
    assert "consent_state != 'declined'" in sql
    assert params == (START, END, [EVENT_LANDING_VIEWED])
    assert counts == {EVENT_LANDING_VIEWED: 42}


@pytest.mark.unit
def test_count_events_by_name_authoritative_skips_consent_filter() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([])
    repo.count_events_by_name(
        conn,
        start=START,
        end=END,
        event_names=(EVENT_LEAD_PERSISTED,),
        authoritative_only=True,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "consent_state" not in sql


@pytest.mark.unit
def test_attribution_summary_sql_is_bounded() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn(
        [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "launch",
                "landing_views": 10,
                "leads": 2,
                "payments": 1,
            }
        ]
    )
    rows = repo.list_attribution_summary(conn, start=START, end=END, limit=25)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert "LIMIT %s" in sql
    assert "occurred_at >=" in sql
    assert "project_briefs" in sql
    assert params[-1] == 25
    assert rows[0]["utm_source"] == "linkedin"


@pytest.mark.unit
def test_content_engagement_sql_uses_allowlisted_slug() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([{"slug": "brave", "views": 7}])
    rows = repo.list_content_engagement(
        conn,
        start=START,
        end=END,
        event_name=EVENT_CASE_STUDY_VIEWED,
        slug_property="case_study_slug",
        limit=20,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert "properties->>%s" in sql
    assert "LIMIT %s" in sql
    assert params[-1] == 20
    assert rows == [{"slug": "brave", "views": 7}]


@pytest.mark.unit
def test_content_engagement_rejects_unknown_slug_property() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.list_content_engagement(
            _mock_conn(),
            start=START,
            end=END,
            event_name=EVENT_INSIGHT_VIEWED,
            slug_property="visitor_id",
            limit=5,
        )


@pytest.mark.unit
def test_load_dashboard_merges_repo_counts() -> None:
    repo = MagicMock()
    repo.count_events_by_name.side_effect = [
        {EVENT_LANDING_VIEWED: 100},
        {},
        {EVENT_LEAD_PERSISTED: 5},
    ]
    repo.list_attribution_summary.return_value = []
    repo.list_content_engagement.return_value = []

    data = load_marketing_analytics_dashboard(
        MagicMock(),
        repo,
        date_from="2026-07-09",
        date_to="2026-07-15",
        now=NOW,
    )
    assert data.engagement_counts[0].count == 100
    assert data.server_counts[0].count == 5
    assert data.conversion_rates[0].denominator == 100


@pytest.mark.unit
def test_dashboard_has_data() -> None:
    empty = empty_marketing_analytics_dashboard(now=NOW)
    assert dashboard_has_data(empty) is False
    populated = replace(
        empty,
        engagement_counts=(replace(empty.engagement_counts[0], count=1),),
    )
    assert dashboard_has_data(populated) is True
