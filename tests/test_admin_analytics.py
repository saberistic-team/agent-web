"""Admin marketing analytics dashboard route tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app
from app.marketing_analytics_dashboard import (
    BriefFunnelCounts,
    EventCountRow,
    MarketingAnalyticsDashboardData,
    parse_analytics_date_range,
)
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_analytics_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "revoked_at": None,
    }


def _preview_dashboard() -> MarketingAnalyticsDashboardData:
    from app.admin_preview import build_preview_marketing_analytics_data

    return build_preview_marketing_analytics_data(
        date_range=parse_analytics_date_range(days=7, now=NOW),
    )


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers.get("location", "")


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_renders_dashboard() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    preview = _preview_dashboard()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=preview,
            ),
        ):
            response = client.get(
                "/admin/analytics?days=7",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title"' in body
    assert "Funnel &amp; attribution" in body or "Funnel & attribution" in body
    assert "Browser engagement" in body
    assert "Server conversions" in body
    assert "Conversion rates" in body
    assert "Attribution" in body
    assert "Case study views" in body
    assert "Export CSV" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_preview_mode_has_mock_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get(
        "/admin/analytics",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Preview data" in body
    assert "Landing Viewed" in body
    assert "linkedin" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_csv_export_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_csv_export_aggregates_only() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    window = parse_analytics_date_range(days=7, now=NOW)
    data = MarketingAnalyticsDashboardData(
        date_range=window,
        engagement_events=(EventCountRow("Landing Viewed", 12, "browser"),),
        server_events=(),
        brief_funnel=BriefFunnelCounts(leads=2, checkouts_opened=1, payments=1),
        conversion_rates=(),
        attribution=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=data,
            ),
        ):
            response = client.get(
                "/admin/analytics/export.csv?days=7",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    body = response.text
    assert "anonymous_session_id" not in body
    assert "browser_engagement" in body
    assert "Landing Viewed" in body
    assert "attachment" in response.headers.get("content-disposition", "")
