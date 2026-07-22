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
from app.admin_analytics_pages import render_marketing_analytics_page
from app.main import app
from app.marketing_analytics_dashboard import (
    AttributionRow,
    ContentEngagementRow,
    ConversionRate,
    EventCount,
    empty_marketing_analytics_dashboard,
)
from app.analytics_event_schema import (
    EVENT_BRIEF_FORM_STARTED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LANDING_VIEWED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    EVENT_SERVICES_VIEWED,
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


def _populated_dashboard():
    base = empty_marketing_analytics_dashboard(now=NOW)
    engagement = tuple(
        EventCount(
            event_name=name,
            label=label,
            count=count,
            source="browser",
        )
        for name, label, count in (
            (EVENT_LANDING_VIEWED, "Landing", 120),
            (EVENT_SERVICES_VIEWED, "Services", 45),
            (EVENT_BRIEF_FORM_STARTED, "Brief form started", 30),
        )
    )
    server = (
        EventCount(EVENT_LEAD_PERSISTED, "Lead persisted", 12, "server"),
        EventCount(EVENT_CHECKOUT_OPENED, "Checkout opened", 8, "server"),
        EventCount(EVENT_PAYMENT_COMPLETED, "Paid diagnostic", 5, "server"),
    )
    return base.__class__(
        engagement_counts=engagement,
        server_counts=server,
        supplementary_counts=base.supplementary_counts,
        conversion_rates=(
            ConversionRate(
                label="Brief start rate",
                numerator=30,
                denominator=120,
                rate_pct=25.0,
                numerator_label="Brief form started",
                denominator_label="Landing views",
                numerator_definition="analytics_events Brief Form Started",
                denominator_definition="analytics_events Landing Viewed",
            ),
        ),
        attribution_rows=(
            AttributionRow(
                utm_source="linkedin",
                utm_medium="social",
                utm_campaign="launch",
                landing_views=80,
                leads=10,
                payments=4,
            ),
        ),
        article_engagement=(ContentEngagementRow("postgres-indexing", 22, "article"),),
        case_study_engagement=(ContentEngagementRow("fintech-replatform", 14, "case_study"),),
        date_range=base.date_range,
        generated_at=NOW,
        metric_definitions=base.metric_definitions,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code in {302, 303}
    assert "/admin/login" in response.headers.get("location", "")


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_renders_populated_dashboard() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _populated_dashboard()

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=dashboard,
            ),
        ):
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})

    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title">Analytics</h1>' in body
    assert "Browser engagement" in body
    assert "Server conversions" in body
    assert "Conversion rates" in body
    assert "Attribution" in body
    assert "Top insights" in body
    assert "postgres-indexing" in body
    assert "linkedin" in body
    assert "25.0%" in body or "25%" in body
    assert 'class="admin-nav-link" aria-current="page">Analytics</a>' in body


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_export_csv_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code in {302, 303}


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_export_csv_returns_aggregates() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    dashboard = _populated_dashboard()

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row),
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
    assert "text/csv" in response.headers.get("content-type", "")
    body = response.text
    assert body.startswith("section,metric,numerator,denominator,rate_pct,count")
    assert "engagement,Landing" in body
    assert "attribution,combined" in body
    assert "content_article,postgres-indexing" in body


@pytest.mark.unit
def test_render_page_shows_zero_denominator_as_dash() -> None:
    data = empty_marketing_analytics_dashboard(now=NOW)
    html = render_marketing_analytics_page(data=data, admin_username=TEST_USERNAME)
    assert "—" in html
    assert "Conversion rates" in html


@pytest.mark.unit
@pytest.mark.integration
def test_analytics_db_error_banner() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)

    with mock_db_connection():
        with (
            patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                side_effect=RuntimeError("db down"),
            ),
        ):
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})

    assert response.status_code == 200
    assert "temporarily unavailable" in response.text
