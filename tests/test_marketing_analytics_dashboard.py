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
from app.analytics_event_schema import EVENT_LANDING_VIEWED, EVENT_PAYMENT_COMPLETED
from app.marketing_analytics_dashboard import (
    AttributionRow,
    ContentEngagementRow,
    ConversionRateRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    normalize_filters,
)
from app.marketing_analytics_export import render_marketing_analytics_export_csv
from app.marketing_analytics_pages import render_marketing_analytics_page
from app.main import app

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
    with patch("app.admin_analytics_routes.db.db_connection") as db_conn:
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


def _dashboard_data() -> MarketingAnalyticsDashboardData:
    filters = normalize_filters(
        date_from="2026-07-09",
        date_to="2026-07-15",
        reference=NOW,
    )
    return MarketingAnalyticsDashboardData(
        filters=filters,
        engagement_events=(
            EventCountRow(EVENT_LANDING_VIEWED, 120, "browser"),
            EventCountRow("Services Viewed", 45, "browser"),
            EventCountRow("Brief Form Started", 12, "browser"),
            EventCountRow("Contact Initiated", 3, "browser"),
        ),
        server_events=(
            EventCountRow("Lead Persisted", 5, "server"),
            EventCountRow("Checkout Opened", 4, "server"),
            EventCountRow(EVENT_PAYMENT_COMPLETED, 3, "server"),
        ),
        conversion_rates=(
            ConversionRateRow(
                "Landing → lead",
                5,
                120,
                4.2,
                "Count of `Lead Persisted` server events",
                "Count of `Landing Viewed` browser events",
            ),
        ),
        attribution=(
            AttributionRow("linkedin", "social", "launch", 40, 3, 2),
        ),
        case_study_views=(ContentEngagementRow("northwind-labs", 18),),
        article_views=(ContentEngagementRow("first-party-analytics", 11),),
        generated_at=NOW,
    )


@pytest.mark.unit
def test_render_marketing_analytics_page_includes_sections() -> None:
    html = render_marketing_analytics_page(
        data=_dashboard_data(),
        admin_username=TEST_USERNAME,
    )
    assert "Funnel &amp; attribution" in html
    assert "Browser engagement" in html
    assert "Server conversions" in html
    assert "Conversion rates" in html
    assert "UTM attribution" in html
    assert "Case study views" in html
    assert "Insight views" in html
    assert EVENT_LANDING_VIEWED in html
    assert EVENT_PAYMENT_COMPLETED in html
    assert "northwind-labs" in html
    assert "Export CSV" in html


@pytest.mark.unit
def test_render_marketing_analytics_page_shows_zero_denominator_dash() -> None:
    data = _dashboard_data()
    data = MarketingAnalyticsDashboardData(
        filters=data.filters,
        engagement_events=data.engagement_events,
        server_events=data.server_events,
        conversion_rates=(
            ConversionRateRow(
                "Checkout → payment",
                0,
                0,
                None,
                "numerator",
                "denominator",
            ),
        ),
        attribution=data.attribution,
        case_study_views=data.case_study_views,
        article_views=data.article_views,
        generated_at=data.generated_at,
    )
    html = render_marketing_analytics_page(data=data, admin_username=TEST_USERNAME)
    assert "—" in html


@pytest.mark.unit
def test_marketing_analytics_export_csv_is_aggregated_only() -> None:
    csv_text = render_marketing_analytics_export_csv(_dashboard_data())
    assert "anonymous_session_id" not in csv_text
    assert "attribution,utm_source" in csv_text.replace("\n", ",")
    assert "Landing Viewed" in csv_text
    assert "northwind-labs" in csv_text


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
                "app.admin_analytics_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=_dashboard_data(),
            ),
        ):
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert "Browser engagement" in response.text
    assert "linkedin" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_authenticated() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_analytics_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=_dashboard_data(),
            ),
            patch("app.admin_analytics_routes._crm.request_export", return_value={}),
        ):
            response = client.get(
                "/admin/analytics/export.csv?date_from=2026-07-09&date_to=2026-07-15",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Landing Viewed" in response.text
