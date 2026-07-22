"""Unit tests for marketing analytics admin routes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.analytics_dashboard import AnalyticsDashboardData, AnalyticsDateRange
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> dict[str, Any]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in _session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_analytics_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/analytics", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_analytics_redirects_to_login() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_dashboard_renders_for_authenticated_user(
    authenticated_admin: dict[str, Any],
) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    empty = AnalyticsDashboardData(
        date_range=AnalyticsDateRange(start=now, end=now, label="Last 7 days (UTC)"),
        engagement_events=(),
        conversion_events=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=now,
    )
    with patch("app.admin_analytics_routes.load_analytics_dashboard", return_value=empty):
        response = client.get("/admin/analytics", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert 'id="analytics-title"' in response.text
    assert "Engagement events" in response.text
    assert "Authoritative conversions" in response.text
    assert "Conversion rates" in response.text
    assert "Attribution (UTM)" in response.text
    assert "Export CSV" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_export_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_export_returns_csv(authenticated_admin: dict[str, Any]) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    empty = AnalyticsDashboardData(
        date_range=AnalyticsDateRange(start=now, end=now, label="Last 7 days (UTC)"),
        engagement_events=(),
        conversion_events=(),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=now,
    )
    with patch("app.admin_analytics_routes.load_analytics_dashboard", return_value=empty):
        response = client.get(
            "/admin/analytics/export.csv?period=7d",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "section,metric,value" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_preview_mode_renders_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get(
        "/admin/analytics",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Landing Viewed" in response.text
    assert "Lead Persisted" in response.text
    assert "meridian-stack" in response.text or "pipeline-signals" in response.text
