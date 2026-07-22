"""Tests for the marketing analytics admin dashboard (#116)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_analytics_pages import render_analytics_dashboard_csv, render_analytics_dashboard_page
from app.admin_auth import SESSION_COOKIE_NAME
from app.analytics_dashboard import (
    AnalyticsDashboardData,
    AttributionRow,
    ContentEngagementRow,
    ConversionRate,
    EventCount,
    parse_date_range,
)
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


def _populated_dashboard() -> AnalyticsDashboardData:
    engagement = (
        EventCount("Landing Viewed", "Landing viewed", 120, "browser"),
        EventCount("Services Viewed", "Services viewed", 45, "browser"),
        EventCount("Case Studies Viewed", "Case studies index viewed", 30, "browser"),
        EventCount("Case Study Viewed", "Case study viewed", 22, "browser"),
        EventCount("Insights Viewed", "Insights index viewed", 18, "browser"),
        EventCount("Insight Viewed", "Insight viewed", 14, "browser"),
        EventCount("Brief Viewed", "Brief viewed", 36, "browser"),
        EventCount("Brief Form Started", "Brief form started", 15, "browser"),
        EventCount("Contact Initiated", "Contact initiated", 6, "browser"),
    )
    server = (
        EventCount("Lead Persisted", "Lead persisted", 10, "server"),
        EventCount("Checkout Opened", "Checkout opened", 7, "server"),
        EventCount("Payment Completed", "Payment completed", 4, "server"),
    )
    return AnalyticsDashboardData(
        engagement_counts=engagement,
        server_counts=server,
        conversion_rates=(
            ConversionRate(
                key="brief_to_form",
                label="Brief view → form start",
                numerator=15,
                denominator=36,
                numerator_label="Brief form started",
                denominator_label="Brief viewed",
                numerator_source="browser",
                denominator_source="browser",
                rate_pct=41.7,
            ),
        ),
        attribution=(
            AttributionRow(
                source="linkedin",
                medium="social",
                campaign="spring-launch",
                total_events=40,
                leads=6,
            ),
        ),
        case_studies=(
            ContentEngagementRow(slug="payments-platform", content_type="case_study", views=12),
        ),
        articles=(
            ContentEngagementRow(slug="diagnostic-readiness", content_type="article", views=8),
        ),
        generated_at=NOW,
        date_range=parse_date_range(date_from="2026-07-01", date_to="2026-07-15", now=NOW),
    )


@pytest.mark.unit
def test_render_populated_analytics_dashboard() -> None:
    html = render_analytics_dashboard_page(
        data=_populated_dashboard(),
        admin_username=TEST_USERNAME,
    )
    assert "Analytics" in html
    assert "Browser engagement" in html
    assert "Server conversions" in html
    assert "Conversion rates" in html
    assert "Attribution" in html
    assert "Case study engagement" in html
    assert "Article engagement" in html
    assert "Landing viewed" in html
    assert "Lead persisted" in html
    assert "linkedin" in html
    assert "payments-platform" in html
    assert "analytics-source-server" in html


@pytest.mark.unit
def test_render_analytics_csv_is_aggregated_only() -> None:
    csv_body = render_analytics_dashboard_csv(_populated_dashboard())
    assert "section,metric,value,detail" in csv_body
    assert "engagement,Landing viewed,120,browser" in csv_body
    assert "server_conversion,Lead persisted,10,server" in csv_body
    assert "attribution,linkedin,40," in csv_body
    assert "anonymous_session_id" not in csv_body


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_requires_authentication() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_dashboard_route_renders_populated_data() -> None:
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
                "app.admin_analytics_routes.load_analytics_dashboard",
                return_value=_populated_dashboard(),
            ),
        ):
            response = client.get(
                "/admin/analytics?from=2026-07-01&to=2026-07-15",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title"' in body
    assert "Conversion rates" in body
    assert 'aria-current="page"' in body


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_export_csv_route() -> None:
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
                "app.admin_analytics_routes.load_analytics_dashboard",
                return_value=_populated_dashboard(),
            ),
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "engagement,Landing viewed,120,browser" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_dashboard_db_error_shows_alert() -> None:
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
                "app.admin_analytics_routes.load_analytics_dashboard",
                side_effect=RuntimeError("db down"),
            ),
        ):
            response = client.get(
                "/admin/analytics",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "temporarily unavailable" in response.text
