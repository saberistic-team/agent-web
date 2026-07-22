"""Integration tests for the admin analytics dashboard (#116)."""

from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_analytics_pages import render_analytics_dashboard_page
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import build_preview_analytics_dashboard_data
from app.analytics_dashboard import AnalyticsDashboardData, build_event_volumes, dashboard_has_activity
from app.analytics_event_schema import EVENT_LANDING_VIEWED
from app.main import app
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


def _sample_dashboard() -> AnalyticsDashboardData:
    counts = {EVENT_LANDING_VIEWED: 42}
    return AnalyticsDashboardData(
        date_from=NOW.date() - timedelta(days=6),
        date_to=NOW.date(),
        event_volumes=build_event_volumes(counts),
        conversion_rates=(),
        attribution_rows=(),
        case_study_engagement=(),
        article_engagement=(),
        generated_at=NOW,
    )


@pytest.mark.unit
def test_preview_analytics_dashboard_seed_stable() -> None:
    a = build_preview_analytics_dashboard_data(rng=random.Random(42), now=NOW)
    b = build_preview_analytics_dashboard_data(rng=random.Random(42), now=NOW)
    assert a == b
    assert dashboard_has_activity(a) is True


@pytest.mark.unit
def test_preview_analytics_dashboard_html_includes_sections() -> None:
    data = build_preview_analytics_dashboard_data(rng=random.Random(99), now=NOW)
    html = render_analytics_dashboard_page(
        data=data,
        admin_username="preview",
        preview_banner="Preview data — not production",
    )
    assert "Preview data — not production" in html
    assert 'id="analytics-title">Analytics</h1>' in html
    assert "Event volume" in html
    assert "Conversion rates" in html
    assert "UTM attribution" in html
    assert "Case study views" in html
    assert "Insight article views" in html
    assert data.attribution_rows[0].utm_source in html
    assert "Export CSV" in html


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_renders_dashboard() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _sample_dashboard()
    with mock_db_connection():
        with (
            patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row),
            patch(
                "app.admin_analytics_routes.load_analytics_dashboard",
                return_value=dashboard,
            ),
        ):
            response = client.get(
                "/admin/analytics?from=2026-07-09&to=2026-07-15",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title">Analytics</h1>' in body
    assert "Landing" in body
    assert 'class="admin-nav-link" aria-current="page">Analytics</a>' in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_returns_aggregated_rows() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _sample_dashboard()
    with mock_db_connection():
        with (
            patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row),
            patch(
                "app.admin_analytics_routes.load_analytics_dashboard",
                return_value=dashboard,
            ),
        ):
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "event_volume" in response.text
    assert "anonymous_session_id" not in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
        response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Conversion rates" in response.text
