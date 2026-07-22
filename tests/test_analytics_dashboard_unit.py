"""Unit tests for marketing analytics dashboard metrics and date parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_dashboard import (
    ALLOWED_PERIOD_DAYS,
    AnalyticsDashboardData,
    AnalyticsDateRange,
    ConversionRateRow,
    DASHBOARD_TIMEZONE,
    METRIC_ATTRIBUTION,
    METRIC_CONVERSION_RATES,
    load_analytics_dashboard,
    parse_analytics_date_range,
    render_analytics_export_csv,
)
from app.analytics_event_schema import (
    EVENT_ABOUT_VIEWED,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.repositories.postgres import PostgresAnalyticsDashboardRepository

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    data = AnalyticsDashboardData(
        date_range=AnalyticsDateRange(start=NOW, end=NOW, label="test"),
        engagement_events=(),
        conversion_events=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert "idempotency_key" in data.metric_definitions["engagement_events"]
    assert "authoritative" in data.metric_definitions["conversion_events"].lower()
    assert "denominator" in data.metric_definitions["conversion_rates"].lower()
    assert "utm_source" in data.metric_definitions["attribution"]
    assert "slug" in data.metric_definitions["content_engagement"].lower()
    assert DASHBOARD_TIMEZONE in data.metric_definitions["engagement_events"] or "UTC" in str(
        data.metric_definitions.values()
    )


@pytest.mark.unit
def test_parse_analytics_date_range_defaults_to_seven_days() -> None:
    parsed = parse_analytics_date_range(reference=NOW)
    assert parsed.label == "Last 7 days (UTC)"
    assert parsed.end == NOW
    assert parsed.start == NOW - timedelta(days=7)


@pytest.mark.unit
@pytest.mark.parametrize("days", sorted(ALLOWED_PERIOD_DAYS))
def test_parse_analytics_date_range_period_presets(days: int) -> None:
    parsed = parse_analytics_date_range(period=f"{days}d", reference=NOW)
    assert parsed.label == f"Last {days} days (UTC)"
    assert (parsed.end - parsed.start).days == days


@pytest.mark.unit
def test_parse_analytics_date_range_custom_dates() -> None:
    parsed = parse_analytics_date_range(start="2026-07-01", end="2026-07-07", reference=NOW)
    assert parsed.label == "2026-07-01 – 2026-07-07 UTC"
    assert parsed.start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert parsed.end == datetime(2026, 7, 8, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_analytics_date_range_rejects_overlong_custom_window() -> None:
    with pytest.raises(ValueError, match="90"):
        parse_analytics_date_range(start="2026-01-01", end="2026-07-15")


@pytest.mark.unit
def test_parse_analytics_date_range_rejects_invalid_period() -> None:
    with pytest.raises(ValueError, match="90"):
        parse_analytics_date_range(period="14d", reference=NOW)


@pytest.mark.unit
def test_load_analytics_dashboard_zero_denominator_rate() -> None:
    repo = MagicMock()
    repo.count_events_in_range.return_value = [
        (EVENT_LANDING_VIEWED, 10),
        (EVENT_BRIEF_VIEWED, 0),
    ]
    repo.count_attribution_in_range.return_value = []
    repo.count_leads_by_utm_source.return_value = []
    repo.count_content_engagement.return_value = []

    data = load_analytics_dashboard(
        MagicMock(),
        repo,
        date_range=AnalyticsDateRange(start=NOW - timedelta(days=7), end=NOW, label="7d"),
        generated_at=NOW,
    )
    landing_to_brief = next(row for row in data.conversion_rates if row.key == "landing_to_brief")
    assert landing_to_brief.denominator == 10
    assert landing_to_brief.numerator == 0
    assert landing_to_brief.rate_pct == 0.0

    empty_den = next(row for row in data.conversion_rates if row.key == "brief_to_form")
    assert empty_den.denominator == 0
    assert empty_den.rate_pct is None


@pytest.mark.unit
def test_load_analytics_dashboard_includes_about_viewed_engagement() -> None:
    repo = MagicMock()
    repo.count_events_in_range.return_value = [
        (EVENT_LANDING_VIEWED, 10),
        (EVENT_ABOUT_VIEWED, 4),
    ]
    repo.count_attribution_in_range.return_value = []
    repo.count_leads_by_utm_source.return_value = []
    repo.count_content_engagement.return_value = []

    data = load_analytics_dashboard(
        MagicMock(),
        repo,
        date_range=AnalyticsDateRange(start=NOW - timedelta(days=7), end=NOW, label="7d"),
        generated_at=NOW,
    )
    engagement_names = [row.event_name for row in data.engagement_events]
    assert EVENT_LANDING_VIEWED in engagement_names
    assert EVENT_ABOUT_VIEWED in engagement_names
    about = next(row for row in data.engagement_events if row.event_name == EVENT_ABOUT_VIEWED)
    assert about.count == 4
    assert about.source == "browser"
    called_names = repo.count_events_in_range.call_args.kwargs["event_names"]
    assert EVENT_ABOUT_VIEWED in called_names


@pytest.mark.unit
def test_load_analytics_dashboard_maps_attribution_and_content() -> None:
    repo = MagicMock()
    repo.count_events_in_range.return_value = [
        (EVENT_LANDING_VIEWED, 5),
        (EVENT_LEAD_PERSISTED, 2),
        (EVENT_BRIEF_FORM_STARTED, 3),
        (EVENT_BRIEF_VIEWED, 4),
        (EVENT_CHECKOUT_OPENED, 1),
        (EVENT_PAYMENT_COMPLETED, 1),
    ]
    repo.count_attribution_in_range.return_value = [
        {"source": "linkedin", "medium": "social", "campaign": "launch", "event_count": 12},
    ]
    repo.count_leads_by_utm_source.return_value = [("linkedin", 3)]
    repo.count_content_engagement.side_effect = [
        [("meridian-stack", 9)],
        [("pipeline-signals", 6)],
    ]

    data = load_analytics_dashboard(
        MagicMock(),
        repo,
        date_range=AnalyticsDateRange(start=NOW - timedelta(days=7), end=NOW, label="7d"),
        generated_at=NOW,
    )
    assert data.attribution_rows[0].lead_count == 3
    assert data.case_study_engagement[0].slug == "meridian-stack"
    assert data.article_engagement[0].slug == "pipeline-signals"
    server_events = {row.event_name for row in data.conversion_events}
    assert EVENT_LEAD_PERSISTED in server_events


@pytest.mark.unit
def test_render_analytics_export_csv_is_aggregated_only() -> None:
    data = AnalyticsDashboardData(
        date_range=AnalyticsDateRange(start=NOW, end=NOW, label="Last 7 days (UTC)"),
        engagement_events=(),
        conversion_events=(),
        conversion_rates=(
            ConversionRateRow(
                key="landing_to_brief",
                label="Landing → brief view",
                numerator=0,
                denominator=0,
                rate_pct=None,
                numerator_definition="Brief Viewed events (browser)",
                denominator_definition="Landing Viewed events (browser)",
            ),
        ),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    csv_text = render_analytics_export_csv(data)
    assert "anonymous_session_id" not in csv_text
    assert "conversion_rate" in csv_text
    assert "Landing → brief view" in csv_text


@pytest.mark.unit
def test_postgres_analytics_repository_event_query_uses_occurred_at_bounds() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    conn = _mock_conn(
        [
            {"event_name": EVENT_LANDING_VIEWED, "total": 4},
        ]
    )
    start = NOW - timedelta(days=7)
    rows = repo.count_events_in_range(
        conn,
        period_start=start,
        period_end=NOW,
        event_names=(EVENT_LANDING_VIEWED,),
    )
    assert rows == [(EVENT_LANDING_VIEWED, 4)]
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "occurred_at >=" in sql
    assert "occurred_at <" in sql
    assert "event_name = ANY" in sql


@pytest.mark.unit
def test_postgres_analytics_repository_rejects_unknown_slug_property() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.count_content_engagement(
            MagicMock(),
            period_start=NOW,
            period_end=NOW,
            event_name="Case Study Viewed",
            slug_property="visitor_id",
            limit=5,
        )
