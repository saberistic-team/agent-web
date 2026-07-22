"""Tests for marketing analytics admin routes (#116)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_analytics_pages import render_marketing_analytics_page
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app
from app.marketing_analytics_dashboard import (
    AnalyticsDateRange,
    AttributionRow,
    ContentEngagementRow,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    compute_conversion_rates,
    parse_analytics_date_range,
)

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


def _sample_dashboard() -> MarketingAnalyticsDashboardData:
    dr = parse_analytics_date_range(now=NOW)
    event_rows = (
        EventCountRow("Landing views", "Landing Viewed", 100, False),
        EventCountRow("Leads persisted", "Lead Persisted", 10, True),
    )
    return MarketingAnalyticsDashboardData(
        date_range=dr,
        event_counts=event_rows,
        conversion_rates=compute_conversion_rates(
            {row.event_name: row.count for row in event_rows}
        ),
        attribution=(
            AttributionRow("linkedin", "social", "launch", 12),
        ),
        case_study_engagement=(ContentEngagementRow("platform-migration", 9),),
        insight_engagement=(ContentEngagementRow("first-party-analytics", 7),),
        generated_at=NOW,
    )


@pytest.mark.unit
def test_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_dashboard_renders_for_authenticated_session() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _sample_dashboard()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=dashboard,
            ),
        ):
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title"' in body
    assert "Conversion rates" in body
    assert "UTM attribution" in body
    assert "Case study engagement" in body
    assert "Server" in body
    assert "Browser" in body


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_dashboard_accepts_date_range_params() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _sample_dashboard()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=dashboard,
            ) as load_mock,
        ):
            response = client.get(
                "/admin/analytics",
                params={"date_from": "2026-07-01", "date_to": "2026-07-14"},
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    load_mock.assert_called_once()
    date_range = load_mock.call_args.kwargs["date_range"]
    assert isinstance(date_range, AnalyticsDateRange)
    assert date_range.date_from_raw == "2026-07-01"
    assert date_range.date_to_raw == "2026-07-14"


@pytest.mark.unit
def test_analytics_csv_export_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_csv_export_returns_aggregated_csv() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _sample_dashboard()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=dashboard,
            ),
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "Marketing analytics export" in body
    assert "Landing Viewed" in body
    assert "linkedin" in body


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_preview_mode_uses_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SERVER_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/analytics")
    assert response.status_code == 200
    body = response.text
    assert "Preview data" in body
    assert 'id="analytics-title"' in body
    assert "Conversion rates" in body
    assert "Leads persisted" in body


@pytest.mark.unit
def test_render_marketing_analytics_page_includes_metric_definitions() -> None:
    html = render_marketing_analytics_page(
        data=_sample_dashboard(),
        admin_username=TEST_USERNAME,
    )
    assert "dashboard-metric-def" in html
    assert "occurred_at" in html
    assert "Numerator" in html
