"""Unit tests for marketing analytics dashboard metrics and repository queries."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
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
    DASHBOARD_TIMEZONE,
    METRIC_ATTRIBUTION,
    METRIC_CONVERSION_RATES,
    METRIC_EVENT_COUNTS,
    AnalyticsDateRange,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    compute_conversion_rates,
    dashboard_has_data,
    empty_dashboard_data,
    load_marketing_analytics_dashboard,
    parse_analytics_date_range,
    render_analytics_export_csv,
)
from app.repositories.postgres import PostgresMarketingAnalyticsRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
DATE_FROM = date(2026, 7, 8)
DATE_TO = date(2026, 7, 14)
RANGE = AnalyticsDateRange(
    start=datetime.combine(DATE_FROM, time.min, tzinfo=timezone.utc),
    end=datetime.combine(DATE_TO + timedelta(days=1), time.min, tzinfo=timezone.utc),
    date_from=DATE_FROM,
    date_to=DATE_TO,
    date_from_raw=DATE_FROM.isoformat(),
    date_to_raw=DATE_TO.isoformat(),
)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    data = empty_dashboard_data(RANGE)
    assert "occurred_at" in data.metric_definitions["event_counts"]
    assert "authoritative" in data.metric_definitions["event_counts"].lower()
    assert "numerator" in data.metric_definitions["conversion_rates"].lower()
    assert METRIC_EVENT_COUNTS == data.metric_definitions["event_counts"]
    assert METRIC_CONVERSION_RATES == data.metric_definitions["conversion_rates"]
    assert METRIC_ATTRIBUTION == data.metric_definitions["attribution"]
    assert DASHBOARD_TIMEZONE in data.metric_definitions["event_counts"]


@pytest.mark.unit
def test_parse_analytics_date_range_defaults_last_seven_days() -> None:
    dr = parse_analytics_date_range(now=NOW)
    assert dr.date_to == NOW.date()
    assert (dr.date_to - dr.date_from).days == 6
    assert dr.start.tzinfo == timezone.utc
    assert dr.end > dr.start


@pytest.mark.unit
def test_parse_analytics_date_range_swaps_inverted_bounds() -> None:
    dr = parse_analytics_date_range(
        date_from="2026-07-14",
        date_to="2026-07-08",
        now=NOW,
    )
    assert dr.date_from == date(2026, 7, 8)
    assert dr.date_to == date(2026, 7, 14)


@pytest.mark.unit
def test_parse_analytics_date_range_caps_at_max_window() -> None:
    dr = parse_analytics_date_range(
        date_from="2026-01-01",
        date_to="2026-07-14",
        now=NOW,
    )
    assert (dr.date_to - dr.date_from).days + 1 <= 90


@pytest.mark.unit
def test_compute_conversion_rates_zero_denominator() -> None:
    rates = compute_conversion_rates(
        {
            EVENT_LANDING_VIEWED: 100,
            EVENT_BRIEF_VIEWED: 0,
        }
    )
    landing_to_brief = next(row for row in rates if row.key == "landing_to_brief")
    assert landing_to_brief.denominator == 100
    assert landing_to_brief.numerator == 0
    assert landing_to_brief.rate_pct == 0.0

    brief_to_form = next(row for row in rates if row.key == "brief_to_form")
    assert brief_to_form.rate_pct is None


@pytest.mark.unit
def test_compute_conversion_rates_rounds_to_one_decimal() -> None:
    rates = compute_conversion_rates(
        {
            EVENT_BRIEF_FORM_STARTED: 1,
            EVENT_LEAD_PERSISTED: 1,
            EVENT_CHECKOUT_OPENED: 1,
            EVENT_PAYMENT_COMPLETED: 1,
            EVENT_BRIEF_VIEWED: 3,
        }
    )
    form_to_lead = next(row for row in rates if row.key == "form_to_lead")
    assert form_to_lead.rate_pct == 100.0


@pytest.mark.unit
def test_dashboard_has_data() -> None:
    empty = empty_dashboard_data(RANGE)
    assert not dashboard_has_data(empty)
    populated = MarketingAnalyticsDashboardData(
        date_range=RANGE,
        event_counts=(
            EventCountRow(
                label="Landing",
                event_name=EVENT_LANDING_VIEWED,
                count=1,
                authoritative=False,
            ),
        ),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        insight_engagement=(),
        generated_at=NOW,
    )
    assert dashboard_has_data(populated)


@pytest.mark.unit
def test_load_marketing_analytics_dashboard_from_repo() -> None:
    repo = MagicMock()
    repo.count_events_by_name.return_value = [
        (EVENT_LANDING_VIEWED, 50),
        (EVENT_BRIEF_VIEWED, 20),
        (EVENT_BRIEF_FORM_STARTED, 10),
        (EVENT_LEAD_PERSISTED, 8),
        (EVENT_CHECKOUT_OPENED, 6),
        (EVENT_PAYMENT_COMPLETED, 4),
    ]
    repo.list_attribution_breakdown.return_value = [
        {
            "utm_source": "linkedin",
            "utm_medium": "social",
            "utm_campaign": "launch",
            "event_count": 12,
        }
    ]
    repo.list_content_engagement.side_effect = [
        [{"slug": "platform-migration", "view_count": 9}],
        [{"slug": "first-party-analytics", "view_count": 7}],
    ]
    conn = _mock_conn()
    data = load_marketing_analytics_dashboard(conn, repo, date_range=RANGE, now=NOW)
    assert data.event_counts[0].count == 50
    assert data.attribution[0].utm_source == "linkedin"
    assert data.case_study_engagement[0].slug == "platform-migration"
    assert data.conversion_rates
    repo.count_events_by_name.assert_called_once()
    assert repo.list_content_engagement.call_count == 2


@pytest.mark.unit
def test_postgres_count_events_by_name_query() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 3}])
    rows = repo.count_events_by_name(
        conn,
        start=RANGE.start,
        end=RANGE.end,
        event_names=(EVENT_LANDING_VIEWED,),
    )
    assert rows == [(EVENT_LANDING_VIEWED, 3)]
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "analytics_events" in sql
    assert "occurred_at >=" in sql


@pytest.mark.unit
def test_postgres_list_content_engagement_rejects_unknown_slug() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    conn = _mock_conn()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.list_content_engagement(
            conn,
            start=RANGE.start,
            end=RANGE.end,
            event_name="Case Study Viewed",
            slug_property="evil_slug",
            limit=10,
        )


@pytest.mark.unit
def test_render_analytics_export_csv_aggregated_only() -> None:
    data = empty_dashboard_data(RANGE)
    csv_body = render_analytics_export_csv(data)
    assert "Marketing analytics export" in csv_body
    assert "date_from" in csv_body
    assert "anonymous_session" not in csv_body.lower()
    assert "Event counts" in csv_body
    assert "Conversion rates" in csv_body
    assert "Attribution" in csv_body
