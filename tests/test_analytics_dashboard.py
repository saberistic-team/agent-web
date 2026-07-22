"""Unit tests for marketing analytics dashboard metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.analytics_dashboard import (
    ALLOWED_RANGE_PRESETS,
    AnalyticsDateRange,
    build_conversion_rates,
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


def _mock_conn(rows: list | dict | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(rows, list):
        cur.fetchall.return_value = rows
    elif isinstance(rows, dict):
        cur.fetchone.return_value = rows
    return conn


@pytest.mark.unit
def test_format_conversion_rate_handles_zero_denominator() -> None:
    assert format_conversion_rate(5, 0) is None
    assert format_conversion_rate(0, 0) is None
    assert format_conversion_rate(1, 4) == 25.0


@pytest.mark.unit
def test_parse_analytics_date_range_presets() -> None:
    for days in ALLOWED_RANGE_PRESETS:
        date_range = parse_analytics_date_range(days=str(days), now=NOW)
        assert date_range.preset_days == days
        assert date_range.end > date_range.start
        assert (date_range.end - date_range.start).days == days


@pytest.mark.unit
def test_parse_analytics_date_range_custom_bounded() -> None:
    date_range = parse_analytics_date_range(
        start="2026-07-01",
        end="2026-07-07",
        now=NOW,
    )
    assert date_range.preset_days is None
    assert date_range.start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert date_range.end == datetime(2026, 7, 8, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_analytics_date_range_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="days must be one of"):
        parse_analytics_date_range(days="14", now=NOW)
    with pytest.raises(ValueError, match="end must be on or after start"):
        parse_analytics_date_range(start="2026-07-10", end="2026-07-01", now=NOW)
    with pytest.raises(ValueError, match="Both start and end"):
        parse_analytics_date_range(start="2026-07-01", now=NOW)


@pytest.mark.unit
def test_build_conversion_rates_uses_explicit_counts() -> None:
    rates = build_conversion_rates(
        event_counts={
            EVENT_LANDING_VIEWED: 100,
            EVENT_BRIEF_VIEWED: 40,
            EVENT_BRIEF_FORM_STARTED: 10,
            EVENT_LEAD_PERSISTED: 8,
            EVENT_CHECKOUT_OPENED: 5,
            EVENT_PAYMENT_COMPLETED: 2,
        },
        crm_counts=type("C", (), {"leads": 8, "checkouts": 5, "payments": 2})(),
    )
    landing_to_brief = rates[0]
    assert landing_to_brief.numerator == 40
    assert landing_to_brief.denominator == 100
    assert landing_to_brief.rate_percent == 40.0
    crm_rate = rates[-1]
    assert crm_rate.numerator == 2
    assert crm_rate.denominator == 8


@pytest.mark.unit
def test_load_analytics_dashboard_maps_repository_rows() -> None:
    repo = MagicMock()
    repo.count_events_by_name.return_value = {EVENT_LANDING_VIEWED: 12}
    repo.count_crm_funnel.return_value = {"leads": 3, "checkouts": 2, "payments": 1}
    repo.list_attribution_buckets.return_value = [
        {"key": "linkedin", "event_count": 9, "lead_count": 2}
    ]
    repo.list_content_engagement.side_effect = [
        [{"slug": "meridian-stack", "views": 7}],
        [{"slug": "platform-risk", "views": 4}],
    ]
    date_range = AnalyticsDateRange(
        start=NOW - timedelta(days=7),
        end=NOW,
        preset_days=7,
    )
    data = load_analytics_dashboard(MagicMock(), repo, date_range=date_range, now=NOW)
    assert data.crm_counts.leads == 3
    assert data.attribution[0].key == "linkedin"
    assert data.case_study_engagement[0].slug == "meridian-stack"
    assert any(row.event_name == EVENT_LANDING_VIEWED for row in data.event_counts)


@pytest.mark.unit
def test_postgres_analytics_dashboard_repository_queries_use_bounds() -> None:
    repo = PostgresAnalyticsDashboardRepository()
    start = NOW - timedelta(days=7)
    end = NOW
    conn = _mock_conn(
        [
            {"event_name": EVENT_LANDING_VIEWED, "total": 4},
        ]
    )
    counts = repo.count_events_by_name(
        conn,
        start=start,
        end=end,
        event_names=[EVENT_LANDING_VIEWED],
    )
    assert counts[EVENT_LANDING_VIEWED] == 4
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "occurred_at >= %s" in sql
    assert "occurred_at < %s" in sql

    conn = _mock_conn({"leads": 2, "checkouts": 1, "payments": 1})
    crm = repo.count_crm_funnel(conn, start=start, end=end)
    assert crm["leads"] == 2

    with pytest.raises(ValueError, match="unsupported attribution dimension"):
        repo.list_attribution_buckets(
            conn,
            start=start,
            end=end,
            dimension="utm_foo",
            limit=5,
        )


@pytest.mark.unit
def test_render_analytics_dashboard_csv_is_aggregated_only() -> None:
    date_range = parse_analytics_date_range(days="7", now=NOW)
    data = load_analytics_dashboard(
        MagicMock(),
        MagicMock(
            count_events_by_name=MagicMock(return_value={EVENT_LANDING_VIEWED: 1}),
            count_crm_funnel=MagicMock(return_value={"leads": 1, "checkouts": 0, "payments": 0}),
            list_attribution_buckets=MagicMock(return_value=[]),
            list_content_engagement=MagicMock(return_value=[]),
        ),
        date_range=date_range,
        now=NOW,
    )
    csv_text = render_analytics_dashboard_csv(data)
    assert "section,metric,value,detail" in csv_text
    assert EVENT_LANDING_VIEWED in csv_text
    assert "anonymous_session_id" not in csv_text
