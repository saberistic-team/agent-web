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
    BROWSER_ENGAGEMENT_EVENTS,
    DASHBOARD_TIMEZONE,
    MAX_RANGE_DAYS,
    METRIC_ATTRIBUTION,
    METRIC_CONVERSION_RATE,
    METRIC_ENGAGEMENT_EVENTS,
    AnalyticsDateRange,
    BriefFunnelCounts,
    MarketingAnalyticsDashboardData,
    conversion_rate_percent,
    dashboard_has_data,
    dashboard_to_csv,
    load_marketing_analytics_dashboard,
    parse_analytics_date_range,
)
from app.repositories.postgres import PostgresMarketingAnalyticsRepository

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


class _FakeMarketingRepo:
    def count_events_by_name(self, conn, *, start, end, event_names):
        return [(EVENT_LANDING_VIEWED, 100), (EVENT_BRIEF_VIEWED, 40)]

    def count_brief_funnel(self, conn, *, start, end):
        return {"leads": 10, "checkouts_opened": 6, "payments": 3}

    def list_attribution_summary(self, conn, *, start, end, limit):
        return [
            {
                "source": "linkedin",
                "medium": "social",
                "campaign": "spring-launch",
                "engagement_events": 55,
                "leads": 4,
                "payments": 2,
            }
        ]

    def list_content_engagement(self, conn, *, start, end, event_name, slug_property, limit):
        if slug_property == "case_study_slug":
            return [{"slug": "payments-platform", "views": 22}]
        return [{"slug": "diagnostic-playbook", "views": 15}]


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    window = parse_analytics_date_range(days=7, now=NOW)
    data = MarketingAnalyticsDashboardData(
        date_range=window,
        engagement_events=(),
        server_events=(),
        brief_funnel=BriefFunnelCounts(leads=0, checkouts_opened=0, payments=0),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert DASHBOARD_TIMEZONE in METRIC_ENGAGEMENT_EVENTS
    assert "authoritative" in data.metric_definitions["server_events"]
    assert METRIC_CONVERSION_RATE == data.metric_definitions["conversion_rate"]
    assert "allowlisted" in METRIC_ATTRIBUTION.lower()
    assert "session" not in METRIC_ATTRIBUTION.lower() or "per-session" in METRIC_ATTRIBUTION


@pytest.mark.unit
def test_conversion_rate_percent_handles_zero_denominator() -> None:
    assert conversion_rate_percent(5, 0) is None
    assert conversion_rate_percent(0, 0) is None
    assert conversion_rate_percent(1, 4) == 25.0


@pytest.mark.unit
def test_parse_analytics_date_range_presets_and_custom() -> None:
    preset = parse_analytics_date_range(days=30, now=NOW)
    assert preset.preset_days == 30
    assert preset.end == NOW
    assert preset.start == NOW - timedelta(days=30)

    custom = parse_analytics_date_range(
        date_from="2026-07-01",
        date_to="2026-07-10",
        now=NOW,
    )
    assert custom.from_date == date(2026, 7, 1)
    assert custom.to_date == date(2026, 7, 10)
    assert custom.end - custom.start == timedelta(days=10)


@pytest.mark.unit
def test_parse_analytics_date_range_caps_at_max_days() -> None:
    wide = parse_analytics_date_range(
        date_from="2026-01-01",
        date_to="2026-12-31",
        now=NOW,
    )
    assert (wide.end - wide.start).days <= MAX_RANGE_DAYS


@pytest.mark.unit
def test_load_marketing_analytics_dashboard_maps_fixture_rows() -> None:
    window = AnalyticsDateRange(
        start=NOW - timedelta(days=7),
        end=NOW,
        preset_days=7,
        from_date=None,
        to_date=None,
    )
    data = load_marketing_analytics_dashboard(
        MagicMock(),
        _FakeMarketingRepo(),
        date_range=window,
        now=NOW,
    )
    landing = next(row for row in data.engagement_events if row.event_name == EVENT_LANDING_VIEWED)
    assert landing.count == 100
    assert landing.source == "browser"
    assert data.brief_funnel.leads == 10
    assert data.attribution[0].source == "linkedin"
    assert data.case_study_engagement[0].slug == "payments-platform"
    assert data.conversion_rates
    lead_to_checkout = next(
        row for row in data.conversion_rates if row.name == "Lead → checkout"
    )
    assert lead_to_checkout.numerator == 6
    assert lead_to_checkout.denominator == 10


@pytest.mark.unit
def test_dashboard_has_data_and_csv_aggregates_only() -> None:
    from app.marketing_analytics_dashboard import EventCountRow

    window = parse_analytics_date_range(days=7, now=NOW)
    empty = MarketingAnalyticsDashboardData(
        date_range=window,
        engagement_events=(),
        server_events=(),
        brief_funnel=BriefFunnelCounts(leads=0, checkouts_opened=0, payments=0),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert dashboard_has_data(empty) is False

    populated = MarketingAnalyticsDashboardData(
        date_range=window,
        engagement_events=(EventCountRow(EVENT_LANDING_VIEWED, 10, "browser"),),
        server_events=(),
        brief_funnel=BriefFunnelCounts(leads=0, checkouts_opened=0, payments=0),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    assert dashboard_has_data(populated) is True

    csv_text = dashboard_to_csv(populated)
    assert "anonymous_session_id" not in csv_text
    assert "browser_engagement" in csv_text
    assert EVENT_LANDING_VIEWED in csv_text


@pytest.mark.unit
def test_count_events_by_name_sql_is_bounded() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    start = NOW - timedelta(days=7)
    conn = _mock_conn([{"event_name": EVENT_LANDING_VIEWED, "total": 12}])
    rows = repo.count_events_by_name(
        conn,
        start=start,
        end=NOW,
        event_names=BROWSER_ENGAGEMENT_EVENTS,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert "occurred_at >= %s" in sql
    assert "occurred_at < %s" in sql
    assert "event_name = ANY(%s)" in sql
    assert params[0] == start
    assert params[1] == NOW
    assert rows == [(EVENT_LANDING_VIEWED, 12)]


@pytest.mark.unit
def test_count_brief_funnel_sql() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    start = NOW - timedelta(days=7)
    conn = _mock_conn(
        {"leads": 5, "checkouts_opened": 3, "payments": 2},
    )
    result = repo.count_brief_funnel(conn, start=start, end=NOW)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "FROM project_briefs" in sql
    assert "stripe_session_id IS NOT NULL" in sql
    assert "status = 'paid'" in sql
    assert result == {"leads": 5, "checkouts_opened": 3, "payments": 2}


@pytest.mark.unit
def test_attribution_summary_sql_has_limit() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    start = NOW - timedelta(days=7)
    conn = _mock_conn([])
    repo.list_attribution_summary(conn, start=start, end=NOW, limit=20)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "utm_source" in sql
    assert "utm_medium" in sql
    assert "utm_campaign" in sql
    assert "LIMIT %s" in sql


@pytest.mark.unit
def test_content_engagement_sql_groups_by_slug() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    start = NOW - timedelta(days=7)
    conn = _mock_conn([{"slug": "edge-migration", "views": 9}])
    rows = repo.list_content_engagement(
        conn,
        start=start,
        end=NOW,
        event_name="Case Study Viewed",
        slug_property="case_study_slug",
        limit=10,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "GROUP BY slug" in sql
    assert "LIMIT %s" in sql
    assert rows == [{"slug": "edge-migration", "views": 9}]


@pytest.mark.unit
def test_conversion_rates_use_server_counts_from_fixtures() -> None:
    window = parse_analytics_date_range(days=7, now=NOW)

    class _RepoWithServerCounts(_FakeMarketingRepo):
        def count_events_by_name(self, conn, *, start, end, event_names):
            if EVENT_LEAD_PERSISTED in event_names:
                return [
                    (EVENT_LEAD_PERSISTED, 8),
                    (EVENT_CHECKOUT_OPENED, 5),
                    (EVENT_PAYMENT_COMPLETED, 2),
                ]
            return [
                (EVENT_BRIEF_VIEWED, 50),
                (EVENT_BRIEF_FORM_STARTED, 20),
            ]

        def count_brief_funnel(self, conn, *, start, end):
            return {"leads": 8, "checkouts_opened": 5, "payments": 2}

    data = load_marketing_analytics_dashboard(
        MagicMock(),
        _RepoWithServerCounts(),
        date_range=window,
        now=NOW,
    )
    checkout_to_payment = next(
        row for row in data.conversion_rates if row.name == "Checkout → payment"
    )
    assert checkout_to_payment.rate_percent == 40.0
    form_to_lead = next(
        row for row in data.conversion_rates if row.name == "Form start → lead"
    )
    assert form_to_lead.denominator == 20
    assert form_to_lead.numerator == 8
