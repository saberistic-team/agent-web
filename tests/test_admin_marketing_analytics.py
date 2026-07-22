"""Tests for the marketing analytics admin dashboard (#116)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_marketing_analytics_pages import render_marketing_analytics_page
from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
)
from app.main import app
from app.marketing_analytics import (
    AttributionRow,
    ContentEngagementRow,
    EventCount,
    MarketingAnalyticsData,
    build_conversion_rates,
    load_marketing_analytics,
    marketing_analytics_is_empty,
    parse_period_days,
    render_marketing_analytics_csv,
)

from app.repositories.postgres import PostgresMarketingAnalyticsRepository
from tests.conftest import enable_admin_preview_env


client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_marketing_analytics_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
    }


class FakeMarketingAnalyticsRepository:
    def count_engagement_events(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
        event_names: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        return [
            (EVENT_LANDING_VIEWED, 100),
            (EVENT_BRIEF_VIEWED, 40),
            (EVENT_BRIEF_FORM_STARTED, 20),
        ]

    def count_server_conversion_events(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
        event_names: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        return [
            (EVENT_LEAD_PERSISTED, 10),
            (EVENT_CHECKOUT_OPENED, 8),
            (EVENT_PAYMENT_COMPLETED, 5),
        ]

    def count_attribution_from_events(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
        event_names: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "spring-launch",
                "events": 55,
            }
        ]

    def count_attribution_from_briefs(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "utm_source": "linkedin",
                "utm_medium": "social",
                "utm_campaign": "spring-launch",
                "leads": 6,
                "payments": 3,
            }
        ]

    def count_content_engagement(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
        event_name: str,
        slug_property: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if slug_property == "case_study_slug":
            return [{"slug": "fintech-platform", "views": 22}]
        return [{"slug": "architecture-diagnostic", "views": 17}]

    def count_abandoned_checkouts(
        self,
        conn: Any,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> int:
        return 2


def _populated_data() -> MarketingAnalyticsData:
    period_end = NOW
    period_start = period_end - timedelta(days=7)
    engagement = {
        EVENT_LANDING_VIEWED: 100,
        EVENT_BRIEF_VIEWED: 40,
        EVENT_BRIEF_FORM_STARTED: 20,
    }
    server = {
        EVENT_LEAD_PERSISTED: 10,
        EVENT_CHECKOUT_OPENED: 8,
        EVENT_PAYMENT_COMPLETED: 5,
    }
    return MarketingAnalyticsData(
        period_days=7,
        period_start=period_start,
        period_end=period_end,
        engagement_counts=(
            EventCount(EVENT_LANDING_VIEWED, "Landing views", 100, "browser"),
            EventCount(EVENT_BRIEF_VIEWED, "Brief page views", 40, "browser"),
            EventCount(EVENT_BRIEF_FORM_STARTED, "Brief form starts", 20, "browser"),
        ),
        server_conversion_counts=(
            EventCount(EVENT_LEAD_PERSISTED, "Leads persisted", 10, "server"),
            EventCount(EVENT_CHECKOUT_OPENED, "Checkouts opened", 8, "server"),
            EventCount(EVENT_PAYMENT_COMPLETED, "Payments completed", 5, "server"),
        ),
        conversion_rates=build_conversion_rates(engagement, server),
        attribution_rows=(
            AttributionRow("linkedin", "social", "spring-launch", 55, 6, 3),
        ),
        case_study_engagement=(
            ContentEngagementRow("case_study", "fintech-platform", 22),
        ),
        article_engagement=(
            ContentEngagementRow("article", "architecture-diagnostic", 17),
        ),
        abandoned_checkouts=2,
        generated_at=NOW,
    )


def _empty_data() -> MarketingAnalyticsData:
    period_end = NOW
    return MarketingAnalyticsData(
        period_days=7,
        period_start=period_end - timedelta(days=7),
        period_end=period_end,
        engagement_counts=(),
        server_conversion_counts=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        abandoned_checkouts=0,
        generated_at=NOW,
    )


@pytest.mark.unit
def test_parse_period_days_bounds_window() -> None:
    assert parse_period_days(None) == 7
    assert parse_period_days("30") == 30
    assert parse_period_days("999") == 7
    assert parse_period_days("not-a-number") == 7


@pytest.mark.unit
def test_build_conversion_rates_handles_zero_denominator() -> None:
    rates = build_conversion_rates(
        {EVENT_LANDING_VIEWED: 0, EVENT_BRIEF_VIEWED: 0, EVENT_BRIEF_FORM_STARTED: 0},
        {
            EVENT_LEAD_PERSISTED: 0,
            EVENT_CHECKOUT_OPENED: 0,
            EVENT_PAYMENT_COMPLETED: 0,
        },
    )
    assert rates
    assert all(rate.rate_pct is None for rate in rates)


@pytest.mark.unit
def test_build_conversion_rates_computes_known_fixture() -> None:
    rates = build_conversion_rates(
        {
            EVENT_LANDING_VIEWED: 100,
            EVENT_BRIEF_VIEWED: 40,
            EVENT_BRIEF_FORM_STARTED: 20,
        },
        {
            EVENT_LEAD_PERSISTED: 10,
            EVENT_CHECKOUT_OPENED: 8,
            EVENT_PAYMENT_COMPLETED: 5,
        },
    )
    by_key = {rate.key: rate for rate in rates}
    assert by_key["lead_to_payment"].numerator == 5
    assert by_key["lead_to_payment"].denominator == 10
    assert by_key["lead_to_payment"].rate_pct == 50.0
    assert by_key["landing_to_brief"].rate_pct == 40.0


@pytest.mark.unit
def test_load_marketing_analytics_from_fixture_repo() -> None:
    conn = MagicMock()
    data = load_marketing_analytics(
        conn,
        FakeMarketingAnalyticsRepository(),
        period_days=7,
        now=NOW,
    )
    assert data.engagement_counts[0].count == 100
    assert data.server_conversion_counts[-1].count == 5
    assert data.attribution_rows[0].utm_source == "linkedin"
    assert data.case_study_engagement[0].slug == "fintech-platform"
    assert data.abandoned_checkouts == 2
    assert not marketing_analytics_is_empty(data)


@pytest.mark.unit
def test_render_populated_page_includes_sections() -> None:
    html = render_marketing_analytics_page(
        data=_populated_data(),
        admin_username=TEST_USERNAME,
    )
    assert 'id="marketing-analytics-title"' in html
    assert "Funnel &amp; attribution" in html
    assert "Browser engagement" in html
    assert "Server conversions" in html
    assert "Conversion rates" in html
    assert "UTM attribution" in html
    assert "fintech-platform" in html
    assert "architecture-diagnostic" in html
    assert "Lead Persisted (server)" in html
    assert "/admin/analytics/export.csv" in html


@pytest.mark.unit
def test_render_empty_page_shows_zero_state() -> None:
    html = render_marketing_analytics_page(
        data=_empty_data(),
        admin_username=TEST_USERNAME,
    )
    assert "No events yet" in html
    assert marketing_analytics_is_empty(_empty_data())


@pytest.mark.unit
def test_render_marketing_analytics_csv_is_aggregated_only() -> None:
    csv_text = render_marketing_analytics_csv(_populated_data())
    assert "section,metric,value,source,period_days" in csv_text
    assert "engagement,Landing views,100,browser,7" in csv_text
    assert "server_conversion,Leads persisted,10,server,7" in csv_text
    assert "conversion_rate,Lead → payment,50.0,5/10,7" in csv_text
    assert "attribution,linkedin|social|spring-launch" in csv_text
    assert "content,case_study:fintech-platform,22,browser,7" in csv_text
    assert "checkout,abandoned_checkouts,2,server,7" in csv_text
    assert "email" not in csv_text.lower()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_populated_state() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_marketing_analytics_routes.load_marketing_analytics",
                return_value=_populated_data(),
            ),
        ):
            response = client.get(
                "/admin/analytics?period=30",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "Browser engagement" in response.text
    assert "linkedin" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_db_error_banner() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_marketing_analytics_routes.load_marketing_analytics",
                side_effect=RuntimeError("db down"),
            ),
        ):
            response = client.get(
                "/admin/analytics",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "temporarily unavailable" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_csv_export_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_csv_export_returns_csv() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_marketing_analytics_routes.load_marketing_analytics",
                return_value=_populated_data(),
            ),
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    assert "engagement,Landing views,100,browser,7" in response.text


@pytest.mark.unit
def test_postgres_marketing_analytics_repo_bounded_content_query() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [{"slug": "fintech-platform", "views": 12}]
    repo = PostgresMarketingAnalyticsRepository()
    period_start = NOW - timedelta(days=7)
    rows = repo.count_content_engagement(
        conn,
        period_start=period_start,
        period_end=NOW,
        event_name="Case Study Viewed",
        slug_property="case_study_slug",
        limit=20,
    )
    assert rows[0]["slug"] == "fintech-platform"
    sql = cur.execute.call_args[0][0]
    assert "LIMIT %s" in sql
    assert cur.execute.call_args[0][1][-1] == 20


@pytest.mark.unit
def test_postgres_marketing_analytics_repo_rejects_unknown_slug_property() -> None:
    repo = PostgresMarketingAnalyticsRepository()
    with pytest.raises(ValueError, match="unsupported slug property"):
        repo.count_content_engagement(
            MagicMock(),
            period_start=NOW - timedelta(days=7),
            period_end=NOW,
            event_name="Case Study Viewed",
            slug_property="visitor_id",
            limit=20,
        )


@pytest.mark.unit
@pytest.mark.integration
def test_preview_analytics_renders_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin_auth

    enable_admin_preview_env(monkeypatch)
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(
                "/admin/analytics",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Browser engagement" in response.text
    assert "linkedin" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_analytics_csv_export(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin_auth

    enable_admin_preview_env(monkeypatch)
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    assert "engagement," in response.text
