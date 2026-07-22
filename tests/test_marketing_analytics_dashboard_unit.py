"""Unit tests for marketing analytics dashboard logic."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.marketing_analytics_dashboard import (
    AnalyticsDateRange,
    compute_rate_pct,
    empty_marketing_analytics_dashboard,
    parse_date_range,
    serialize_dashboard_csv,
    _build_conversion_rates,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


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
